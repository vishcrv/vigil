"""
Phase 4 batch rule pass: run the 8 motif rules (ml/rules.py) over the full account graph
once, write account-level hits to data/HI-Small_Rule_Hits.parquet.

Run: python scripts/build_rule_hits.py

Why batch and not per-query: cycle/stack/scatter-gather are multi-hop joins over 1,015,736
unique edges (phase1.md §8) — far too slow to run inside an agent tool call, and a motif that
reaches outside the caller's scope would be invisible if computed scope-locally. anomaly()
joins against this table instead.

Output schema: account, rule, evidence (JSON string), score (0-1).
"""
import time
from pathlib import Path

import duckdb

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ml import rules  # noqa: E402
from ml.data import ENRICHED_PATH  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
OUT_PATH = str(REPO_ROOT / "data" / "HI-Small_Rule_Hits.parquet")


def build_edges(con: duckdb.DuckDBPyConnection) -> None:
    """Normalized graph view. Self-loops excluded — a transfer to yourself has no
    counterparty and cannot form a motif (phase2.md decision #3 kept them flagged, not
    dropped, so this layer could make that call)."""
    con.execute(f"""
        CREATE OR REPLACE TABLE edges AS
        SELECT "From Account" AS src,
               "To Account"   AS dst,
               "Amount Received" AS amt,
               strptime("Timestamp", '%Y/%m/%d %H:%M') AS ts
        FROM read_parquet('{ENRICHED_PATH}')
        WHERE NOT is_self_loop
    """)
    n = con.execute("SELECT count(*) FROM edges").fetchone()[0]
    print(f"  edges: {n:,} (self-loops excluded)")


def main() -> None:
    con = duckdb.connect(":memory:")

    print("building edge graph...")
    t0 = time.time()
    build_edges(con)
    print(f"  {time.time() - t0:.1f}s")

    con.execute("CREATE OR REPLACE TABLE rule_hits (account VARCHAR, rule VARCHAR, "
                "evidence VARCHAR, score DOUBLE)")

    for name, builder in rules.STRUCTURED_RULES.items():
        print(f"running {name}...")
        t0 = time.time()
        con.execute(f"INSERT INTO rule_hits {builder()}")
        n = con.execute("SELECT count(*) FROM rule_hits WHERE rule = ?", [name]).fetchone()[0]
        print(f"  {n:,} accounts hit  ({time.time() - t0:.1f}s)")

    # RANDOM is a set difference over the seven structured rules, so it has to run last.
    print("running RANDOM...")
    t0 = time.time()
    con.execute("CREATE OR REPLACE TABLE structured_hits AS SELECT DISTINCT account FROM rule_hits")
    con.execute(f"INSERT INTO rule_hits {rules.random_sql()}")
    n = con.execute("SELECT count(*) FROM rule_hits WHERE rule = 'RANDOM'").fetchone()[0]
    print(f"  {n:,} accounts hit  ({time.time() - t0:.1f}s)")

    total, accounts = con.execute(
        "SELECT count(*), count(DISTINCT account) FROM rule_hits"
    ).fetchone()
    print(f"\ntotal: {total:,} hits across {accounts:,} distinct accounts")

    con.execute(f"COPY (SELECT * FROM rule_hits ORDER BY account, rule) TO '{OUT_PATH}' (FORMAT PARQUET)")
    print(f"wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
