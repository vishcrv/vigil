"""
Phase 4 model training: fit Isolation Forest + LOF once, persist to data/models/.

Run: python scripts/train_models.py

Unsupervised by design — ml_spec.md Phase 4 step 1: at 0.102% positives (phase1.md §6),
supervised classification is unreliable as the sole method, so `is_laundering` is used here
only to *evaluate* the fitted detectors on a held-out split, never as a training target.

LOF is fitted with novelty=True so it can score rows it has never seen; without that it only
exposes fit-time scores and could not serve an agent query. Its neighbour search is the
expensive part, so it trains on a smaller subsample than the forest.
"""
import json
import sys
import time
from pathlib import Path

import duckdb
import joblib
import numpy as np
from sklearn.ensemble import IsolationForest
from sklearn.metrics import average_precision_score, roc_auc_score
from sklearn.neighbors import LocalOutlierFactor
from sklearn.preprocessing import StandardScaler

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ml.data import ENRICHED_PATH  # noqa: E402
from ml.features import FEATURE_COLUMNS, FEATURE_SQL  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
MODEL_DIR = REPO_ROOT / "data" / "models"

SAMPLE_ROWS = 500_000
TRAIN_ROWS = 300_000
LOF_TRAIN_ROWS = 50_000
SEED = 42


def load_sample() -> tuple[np.ndarray, np.ndarray]:
    con = duckdb.connect(":memory:")
    print(f"sampling {SAMPLE_ROWS:,} rows...")
    df = con.execute(f"""
        SELECT {FEATURE_SQL}, CAST(is_laundering AS INTEGER) AS y
        FROM read_parquet('{ENRICHED_PATH}')
        USING SAMPLE {SAMPLE_ROWS} ROWS (reservoir, {SEED})
    """).df()
    y = df.pop("y").to_numpy()
    df = df[FEATURE_COLUMNS]
    X = df.to_numpy(dtype=np.float64)
    # Guard: the enrichment pass reported no nulls, but a NaN reaching sklearn here would
    # surface as an unhelpful error deep in the tree builder rather than at the boundary.
    if not np.isfinite(X).all():
        n_bad = int((~np.isfinite(X)).any(axis=1).sum())
        raise ValueError(f"{n_bad:,} sampled rows contain non-finite feature values")
    print(f"  positives in sample: {int(y.sum()):,} ({y.mean():.4%})")
    return X, y


def main() -> None:
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    X, y = load_sample()

    X_train, y_train = X[:TRAIN_ROWS], y[:TRAIN_ROWS]
    X_eval, y_eval = X[TRAIN_ROWS:], y[TRAIN_ROWS:]
    print(f"  train {len(X_train):,} / eval {len(X_eval):,} (disjoint)")

    scaler = StandardScaler().fit(X_train)
    Xs_train = scaler.transform(X_train)
    Xs_eval = scaler.transform(X_eval)

    print("fitting IsolationForest...")
    t0 = time.time()
    iforest = IsolationForest(
        n_estimators=200, contamination="auto", random_state=SEED, n_jobs=-1
    ).fit(Xs_train)
    print(f"  {time.time() - t0:.1f}s")

    print(f"fitting LOF (novelty=True) on {LOF_TRAIN_ROWS:,} rows...")
    t0 = time.time()
    lof = LocalOutlierFactor(n_neighbors=20, novelty=True, n_jobs=-1).fit(
        Xs_train[:LOF_TRAIN_ROWS]
    )
    print(f"  {time.time() - t0:.1f}s")

    # score_samples is higher = more normal for both detectors, so negate to get an
    # "outlierness" score that increases with suspicion.
    print("evaluating on held-out split...")
    metrics = {}
    quantiles = {}
    for name, model in (("isolation_forest", iforest), ("lof", lof)):
        t0 = time.time()
        scores = -model.score_samples(Xs_eval)
        # Raw detector scores are unbounded and not comparable across methods. Persisting a
        # quantile grid of this reference population lets anomaly() report each score as a
        # percentile ("more anomalous than X% of transactions"), which is both bounded to
        # [0,1] for combining and directly explainable in Phase 6 templates.
        quantiles[name] = np.quantile(scores, np.linspace(0.0, 1.0, 1001)).tolist()
        metrics[name] = {
            "roc_auc": round(float(roc_auc_score(y_eval, scores)), 4),
            "average_precision": round(float(average_precision_score(y_eval, scores)), 5),
            "score_seconds": round(time.time() - t0, 1),
        }
        print(f"  {name:18} roc_auc={metrics[name]['roc_auc']:.4f}  "
              f"ap={metrics[name]['average_precision']:.5f}  "
              f"({metrics[name]['score_seconds']}s)")

    baseline_ap = float(y_eval.mean())
    print(f"  {'(random baseline)':18} ap={baseline_ap:.5f}")

    # Reference distribution for the z-score method, so all three methods land on the same
    # percentile scale rather than one of them using an ad-hoc constant.
    zs = np.abs(X_eval[:, FEATURE_COLUMNS.index("deviation_from_avg")])
    quantiles["zscore"] = np.quantile(zs, np.linspace(0.0, 1.0, 1001)).tolist()

    joblib.dump(scaler, MODEL_DIR / "scaler.joblib")
    joblib.dump(iforest, MODEL_DIR / "isolation_forest.joblib")
    joblib.dump(lof, MODEL_DIR / "lof.joblib")

    meta = {
        "feature_columns": FEATURE_COLUMNS,
        "sample_rows": SAMPLE_ROWS,
        "train_rows": TRAIN_ROWS,
        "lof_train_rows": LOF_TRAIN_ROWS,
        "seed": SEED,
        "eval_positive_rate": round(baseline_ap, 6),
        "metrics": metrics,
        "score_quantiles": quantiles,
    }
    (MODEL_DIR / "metadata.json").write_text(json.dumps(meta, indent=2))
    print(f"\nwrote {MODEL_DIR}")


if __name__ == "__main__":
    main()
