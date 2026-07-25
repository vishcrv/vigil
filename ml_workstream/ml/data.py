"""
Shared DuckDB connection over the enriched Parquet file. Used by every ML tool function
(feature_eng, eda, anomaly, risk) so there's one place that knows the data path and one
cached connection, per spec.md's "Query: DuckDB directly over Parquet" row.
"""
import os
from functools import lru_cache
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = os.environ.get("DATA_DIR", str(REPO_ROOT / "data"))
# Single enriched table for every tool. There was previously a second byte-identical
# HI-Small_Agent_Ready.parquet (Phase 3 defines no extra precomputed columns, so it was a
# 352 MB copy); the fallback keeps a data directory built by the older script working.
ENRICHED_PATH = os.path.join(DATA_DIR, "HI-Small_Enriched.parquet")
if not os.path.exists(ENRICHED_PATH):
    _legacy = os.path.join(DATA_DIR, "HI-Small_Agent_Ready.parquet")
    if os.path.exists(_legacy):
        ENRICHED_PATH = _legacy


@lru_cache(maxsize=1)
def get_connection() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(database=":memory:")
    con.execute(
        f"CREATE VIEW enriched AS SELECT * FROM read_parquet('{ENRICHED_PATH}')"
    )
    return con
