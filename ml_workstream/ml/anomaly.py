"""
Phase 4 — `anomaly(scope, method?) -> dict` tool function.

Two independent mechanisms, kept separate all the way out to the caller (ml_spec.md Phase 4
step 4): continuous detector scores, and named rule hits from the batch graph pass. They are
deliberately *not* collapsed into one number here — Phase 5 needs both to decide a risk level,
and Phase 6 needs to know which one fired to pick an explanation template.

Contract (ml_spec.md "Interface contract"): plain dict in, plain dict out, never raises,
read-only.
"""
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

from ml.data import ENRICHED_PATH, get_connection
from ml.feature_eng import build_where, validate_scope
from ml.features import FEATURE_COLUMNS, FEATURE_SQL

REPO_ROOT = Path(__file__).resolve().parents[2]
MODEL_DIR = REPO_ROOT / "data" / "models"
RULE_HITS_PATH = REPO_ROOT / "data" / "HI-Small_Rule_Hits.parquet"

METHODS = ("isolation_forest", "lof", "zscore")
MAX_SCORE_ROWS = 5000   # cap on rows scored per call; LOF neighbour search is the bottleneck
TOP_ROWS = 10

_ID_COLUMNS = [
    "Timestamp", "From Account", "To Account", "Amount Received",
    "Payment Format", "aml_pattern", "is_laundering", "is_suspicious",
]


@lru_cache(maxsize=1)
def _load_models() -> dict:
    """Load persisted artifacts once. Returns {} if training has not been run — callers turn
    that into a structured error rather than an exception."""
    import joblib

    required = ["scaler.joblib", "isolation_forest.joblib", "lof.joblib", "metadata.json"]
    if not all((MODEL_DIR / f).exists() for f in required):
        return {}
    meta = json.loads((MODEL_DIR / "metadata.json").read_text())
    return {
        "scaler": joblib.load(MODEL_DIR / "scaler.joblib"),
        "isolation_forest": joblib.load(MODEL_DIR / "isolation_forest.joblib"),
        "lof": joblib.load(MODEL_DIR / "lof.joblib"),
        "metadata": meta,
        "quantiles": {k: np.asarray(v) for k, v in meta["score_quantiles"].items()},
    }


def _to_percentile(scores: np.ndarray, grid: np.ndarray) -> np.ndarray:
    """Map raw scores onto [0,1] against the reference distribution captured at training time.

    A percentile is the only form in which an Isolation Forest score, an LOF score and a
    z-score mean the same thing, which is what makes averaging them into one headline number
    defensible.
    """
    idx = np.searchsorted(grid, scores, side="left")
    return np.clip(idx / (len(grid) - 1), 0.0, 1.0)


def _fetch_rule_hits(con, accounts: list[str]) -> list[dict]:
    if not RULE_HITS_PATH.exists() or not accounts:
        return []
    placeholders = ", ".join("?" for _ in accounts)
    rows = con.execute(
        f"""SELECT account, rule, score, evidence
            FROM read_parquet('{RULE_HITS_PATH.as_posix()}')
            WHERE account IN ({placeholders})
            ORDER BY score DESC""",
        accounts,
    ).fetchall()
    return [
        {"account": a, "rule": r, "score": round(float(s), 4), "evidence": json.loads(e)}
        for a, r, s, e in rows
    ]


def anomaly(scope: dict, method: str = "all") -> dict:
    clean_scope, errors = validate_scope(scope)
    if method not in METHODS + ("all",):
        errors.append(f"method must be one of: {', '.join(METHODS + ('all',))}")
    if errors:
        return {"error": "; ".join(errors), "scope": scope}

    models = _load_models()
    if not models:
        return {
            "error": "models not found - run scripts/train_models.py first",
            "scope": clean_scope,
        }

    try:
        con = get_connection()
        where_sql, params = build_where(clean_scope)
        id_cols = ", ".join(f'"{c}"' for c in _ID_COLUMNS)
        limit = min(clean_scope.get("limit", MAX_SCORE_ROWS), MAX_SCORE_ROWS)

        df = con.execute(
            f"""SELECT {id_cols}, {FEATURE_SQL}
                FROM enriched WHERE {where_sql}
                ORDER BY "Timestamp" DESC LIMIT ?""",
            params + [limit],
        ).df()

        if df.empty:
            return {
                "scope": clean_scope, "method": method, "row_count_scored": 0,
                "anomaly_score": 0.0, "method_scores": {}, "rule_hits": [], "top_rows": [],
                "note": "no transactions matched this scope",
            }

        X = df[FEATURE_COLUMNS].to_numpy(dtype=np.float64)
        Xs = models["scaler"].transform(X)

        wanted = METHODS if method == "all" else (method,)
        per_row: dict[str, np.ndarray] = {}
        for name in wanted:
            if name == "zscore":
                raw = np.abs(df["deviation_from_avg"].to_numpy(dtype=np.float64))
            else:
                raw = -models[name].score_samples(Xs)
            per_row[name] = _to_percentile(raw, models["quantiles"][name])

        # Headline per row is the mean across methods: averaging calibrated percentiles keeps
        # the combined score calibrated too, whereas taking the max would push a scope's score
        # toward 1.0 purely because three detectors were consulted instead of one.
        combined = np.mean(np.vstack(list(per_row.values())), axis=0)

        order = np.argsort(-combined)[:TOP_ROWS]
        top_rows = []
        for i in order:
            row = {c: df.iloc[i][c] for c in _ID_COLUMNS}
            row["Amount Received"] = float(row["Amount Received"])
            row["is_laundering"] = bool(row["is_laundering"])
            row["is_suspicious"] = bool(row["is_suspicious"])
            # Both already present via FEATURE_SQL, surfaced under their natural names so
            # Phase 5 can apply the self-loop / zero-risk-format correction (phase4.md §7)
            # without re-querying. Not added to _ID_COLUMNS — that would duplicate the
            # column in the SELECT and give the frame two columns of the same name.
            row["is_self_loop"] = bool(df.iloc[i]["is_self_loop_i"])
            row["currency_mismatch"] = bool(df.iloc[i]["currency_mismatch_i"])
            row["payment_format_risk"] = float(df.iloc[i]["payment_format_risk"])
            row["anomaly_score"] = round(float(combined[i]), 4)
            row["method_scores"] = {k: round(float(v[i]), 4) for k, v in per_row.items()}
            top_rows.append(row)

        accounts = sorted(
            set(df["From Account"].tolist()) | set(df["To Account"].tolist())
        )
        rule_hits = _fetch_rule_hits(con, accounts)

        return {
            "scope": clean_scope,
            "method": method,
            "row_count_scored": int(len(df)),
            # Scope-level headline is the worst row in scope, not the average: an account with
            # one clearly anomalous transfer among 500 routine ones is exactly the case this
            # tool exists to surface, and a mean would bury it.
            "anomaly_score": round(float(combined.max()), 4),
            "mean_anomaly_score": round(float(combined.mean()), 4),
            "method_scores": {k: round(float(v.max()), 4) for k, v in per_row.items()},
            "rule_hits": rule_hits,
            "rule_names": sorted({h["rule"] for h in rule_hits}),
            "top_rows": top_rows,
        }
    except Exception as exc:
        return {"error": str(exc), "scope": scope}
