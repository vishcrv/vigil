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
# feature_eng (Phase 3) reads the Agent_Ready deliverable, not Enriched directly — see
# scripts/enrich.py, which writes both from the same DataFrame.
ENRICHED_PATH = os.path.join(DATA_DIR, "HI-Small_Agent_Ready.parquet")


@lru_cache(maxsize=1)
def get_connection() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(database=":memory:")
    con.execute(
        f"CREATE VIEW enriched AS SELECT * FROM read_parquet('{ENRICHED_PATH}')"
    )
    return con
