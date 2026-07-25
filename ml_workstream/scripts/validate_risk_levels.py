"""End-to-end validation of the risk levels the pipeline actually emits.

Run: python scripts/validate_risk_levels.py [--launderers N] [--clean N] [--seed N]

Samples accounts from both strata, pushes each one through the real tool surface
(`ml.tools.dispatch` -> `anomaly` -> `risk`, no shortcut, so scope validation, date
normalization and the 5,000-row truncation path are all exercised), and cross-tabulates the
resulting `risk_level` against ground truth.

Writes `data/models/risk_validation.json`. Deliberately a separate artifact rather than a key
inside `metadata.json`: that file is rewritten wholesale by scripts/train_models.py, so a
validation table living there would be silently destroyed by the next retrain.

Ground truth is account-level: an account counts as laundering-involved if it appears on either
side of at least one `is_laundering` transaction. Sender-only would be the stricter reading, but
`risk` scopes accounts with `role="both"`, so the label has to match what the pipeline is
actually being asked about.
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ml.data import get_connection  # noqa: E402
from ml.tools import dispatch  # noqa: E402
from ml.validation import (  # noqa: E402
    RISK_ORDER,
    attach_bootstrap_ci,
    crosstab,
    format_markdown,
    recall_at_or_above,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
OUT_PATH = REPO_ROOT / "data" / "models" / "risk_validation.json"
RECORDS_PATH = REPO_ROOT / "data" / "models" / "risk_validation_records.json"

DEFAULT_LAUNDERERS = 600
DEFAULT_CLEAN = 1400
SEED = 42

_ACCOUNT_SIDES = """
    WITH sides AS (
        SELECT "From Account" AS acct, is_laundering FROM enriched
        UNION ALL
        SELECT "To Account" AS acct, is_laundering FROM enriched
    )
    SELECT acct, max(is_laundering) AS involved
    FROM sides GROUP BY acct
"""


def population_counts(con) -> tuple[int, int]:
    row = con.execute(f"""
        SELECT count(*) FILTER (WHERE involved),
               count(*) FILTER (WHERE NOT involved)
        FROM ({_ACCOUNT_SIDES})
    """).fetchone()
    return int(row[0]), int(row[1])


def sample_accounts(con, involved: bool, n: int, seed: int) -> list[str]:
    """Deterministic pseudo-random draw from one stratum.

    Not `USING SAMPLE`: DuckDB applies that at the table scan, *before* the stratum filter, so
    sampling N rows and then keeping the laundering ones returned an empty positive stratum.
    Hash-ordering filters first and is reproducible across runs for a given seed.
    """
    rows = con.execute(f"""
        SELECT acct FROM ({_ACCOUNT_SIDES})
        WHERE involved = {'TRUE' if involved else 'FALSE'}
        ORDER BY hash(acct || '{seed}')
        LIMIT {n}
    """).fetchall()
    return [r[0] for r in rows]


def score_account(account_id: str, is_laundering: bool, stratum: str) -> dict:
    """One account through the real tool surface. Never raises — a failure is recorded as an
    error record and excluded from the table rather than counted as a clean LOW."""
    scored = dispatch("anomaly", {"scope": {"account_id": account_id}})
    if "error" in scored:
        return {"account_id": account_id, "stratum": stratum,
                "is_laundering": is_laundering, "error": scored["error"]}

    assessed = dispatch("risk", {"anomaly_result": scored})
    if "error" in assessed:
        return {"account_id": account_id, "stratum": stratum,
                "is_laundering": is_laundering, "error": assessed["error"]}

    return {
        "account_id": account_id,
        "stratum": stratum,
        "is_laundering": is_laundering,
        "risk_level": assessed["risk_level"],
        "risk_score": assessed["risk_score"],
        "pattern_detected": assessed["pattern_detected"],
        "anomaly_score": assessed["anomaly_score"],
        "rows_scored": scored["row_count_scored"],
        "truncated": scored["truncated"],
        # Kept so the blend constants can be swept by replaying risk() over cached detector
        # output. Re-scoring 2,000 accounts per candidate setting would make a sweep of any
        # width impossible - this is 15 minutes once instead of 15 minutes per point.
        "anomaly_result": scored,
    }


def collect(n_launderers: int, n_clean: int, seed: int, quiet: bool = False) -> tuple[list[dict], dict]:
    con = get_connection()
    pop_positive, pop_negative = population_counts(con)
    if not quiet:
        print(f"population: {pop_positive:,} laundering-involved / "
              f"{pop_negative:,} clean "
              f"({pop_positive / (pop_positive + pop_negative):.3%} base rate)")

    positives = sample_accounts(con, True, n_launderers, seed)
    negatives = sample_accounts(con, False, n_clean, seed)
    if not quiet:
        print(f"sampling {len(positives):,} + {len(negatives):,} accounts "
              f"through dispatch(anomaly) -> dispatch(risk)...")

    records: list[dict] = []
    started = time.time()
    for i, (account, positive, stratum) in enumerate(
        [(a, True, "laundering") for a in positives]
        + [(a, False, "clean") for a in negatives]
    ):
        records.append(score_account(account, positive, stratum))
        if not quiet and (i + 1) % 200 == 0:
            rate = (i + 1) / (time.time() - started)
            print(f"  {i + 1:,} scored ({rate:.1f}/s)")

    # Horvitz-Thompson: each sampled account stands for population/sample of its own stratum.
    weights = {
        "laundering": pop_positive / len(positives),
        "clean": pop_negative / len(negatives),
    }
    meta = {
        "population_laundering_accounts": pop_positive,
        "population_clean_accounts": pop_negative,
        "sampled_laundering": len(positives),
        "sampled_clean": len(negatives),
        "stratum_weights": {k: round(v, 4) for k, v in weights.items()},
        "seed": seed,
        "elapsed_seconds": round(time.time() - started, 1),
    }
    return records, {"weights": weights, "meta": meta}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--launderers", type=int, default=DEFAULT_LAUNDERERS)
    parser.add_argument("--clean", type=int, default=DEFAULT_CLEAN)
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()

    records, context = collect(args.launderers, args.clean, args.seed)
    weights, meta = context["weights"], context["meta"]

    table = attach_bootstrap_ci(crosstab(records, weights), records, weights)
    print()
    print(format_markdown(table))
    print()

    if table["errors"]:
        print(f"{table['errors']} account(s) failed to score and were excluded")
    if table["unknown_levels"]:
        print(f"unexpected risk levels: {table['unknown_levels']}")

    for level in ("MEDIUM", "HIGH", "CRITICAL"):
        print(f"recall at {level}+: {recall_at_or_above(table, level):.1%}")

    payload = {
        **meta,
        "table": table,
        "recall_at_or_above": {
            level: round(recall_at_or_above(table, level), 5) for level in RISK_ORDER
        },
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, indent=2))
    print(f"\nwrote {OUT_PATH}")

    # Raw scored sample, kept separate from the summary because it is large and only the
    # constant-tuning sweep needs it.
    RECORDS_PATH.write_text(json.dumps({"weights": weights, "records": records}))
    size_mb = RECORDS_PATH.stat().st_size / 1e6
    print(f"wrote {RECORDS_PATH} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
