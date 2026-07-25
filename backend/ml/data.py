"""
Shared DuckDB connection and the single source of truth for every data path.

Used by every ML tool function (feature_eng, eda, anomaly, risk) and by the build scripts, so
there is one place that knows where the data lives and one cached connection, per spec.md's
"Query: DuckDB directly over Parquet" row.

**Every path is derived from `DATA_DIR`.** Modules used to compute their own
`Path(__file__).parents[2] / "data"`, which resolved to the repo root while `DATA_DIR` (per
backend/.env.example) points at `backend/data`. The two disagreed, so `anomaly` looked for its
models somewhere the enrichment pipeline had never written them and failed with "models not
found" while the Parquet loaded fine. Import the constants below; do not rebuild paths.
"""
import os
from functools import lru_cache
from pathlib import Path

import duckdb
from dotenv import load_dotenv

BACKEND_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(BACKEND_ROOT / ".env")

# Relative DATA_DIR values resolve against backend/, not the process working directory: the
# default in .env.example is "./data", and uvicorn is documented as running from backend/, but
# a script invoked from the repo root would otherwise silently look in the wrong place.
_configured = os.environ.get("DATA_DIR") or "./data"
DATA_DIR = str(Path(_configured) if Path(_configured).is_absolute()
               else (BACKEND_ROOT / _configured).resolve())

# Raw Kaggle inputs (gitignored). Only the enrichment script reads these.
RAW_DIR = os.path.join(DATA_DIR, "raw")
TRANS_PATH = os.path.join(RAW_DIR, "HI-Small_Trans.csv")
ACCOUNTS_PATH = os.path.join(RAW_DIR, "HI-Small_accounts.csv")
PATTERNS_PATH = os.path.join(RAW_DIR, "HI-Small_Patterns.txt")

# Derived artifacts, all produced by scripts/ and all gitignored.
ENRICHED_PATH = os.path.join(DATA_DIR, "HI-Small_Enriched.parquet")
RULE_HITS_PATH = os.path.join(DATA_DIR, "HI-Small_Rule_Hits.parquet")
MODEL_DIR = os.path.join(DATA_DIR, "models")


@lru_cache(maxsize=1)
def get_connection() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(database=":memory:")
    con.execute(
        f"CREATE VIEW enriched AS SELECT * FROM read_parquet('{ENRICHED_PATH}')"
    )
    return con
