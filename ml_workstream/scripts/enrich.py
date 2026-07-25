"""
Phase 2 enrichment pipeline. One-time script: raw Kaggle CSVs -> data/HI-Small_Enriched.parquet.

Run: python scripts/enrich.py
Inputs (repo root, gitignored): HI-Small_Trans.csv, HI-Small_accounts.csv, HI-Small_Patterns.txt
Output (gitignored): data/HI-Small_Enriched.parquet

Column list and decisions are documented in ml_spec.md (Phase 2) and phase2.md.
"""
import re
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
TRANS_PATH = str(REPO_ROOT / "HI-Small_Trans.csv")
ACCOUNTS_PATH = str(REPO_ROOT / "HI-Small_accounts.csv")
PATTERNS_PATH = str(REPO_ROOT / "HI-Small_Patterns.txt")
OUT_PATH = str(REPO_ROOT / "data" / "HI-Small_Enriched.parquet")

TRANS_COLS = [
    "Timestamp", "From Bank", "From Account", "To Bank", "To Account",
    "Amount Received", "Receiving Currency", "Amount Paid", "Payment Currency",
    "Payment Format", "Is Laundering",
]

AMOUNT_BUCKET_LABELS = ["micro", "small", "medium", "large", "xlarge", "xxlarge"]


def load_transactions() -> pd.DataFrame:
    df = pd.read_csv(TRANS_PATH)
    df.columns = TRANS_COLS
    return df


def parse_patterns(path: str) -> pd.DataFrame:
    """Parse HI-Small_Patterns.txt into a DataFrame of (type + 11 tx fields) rows,
    one row per data line inside a BEGIN/END block. No shared ID with transactions.csv —
    matched back by exact field-tuple join (see phase1.md §9)."""
    rows = []
    current_type = None
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line.startswith("BEGIN LAUNDERING ATTEMPT"):
                m = re.search(r"BEGIN LAUNDERING ATTEMPT - ([A-Z-]+)", line)
                current_type = m.group(1)
            elif line.startswith("END"):
                current_type = None
            elif line:
                fields = line.split(",")
                if len(fields) == len(TRANS_COLS):
                    rows.append([current_type] + fields)
    pat = pd.DataFrame(rows, columns=["aml_pattern"] + TRANS_COLS)
    for col in ["From Bank", "To Bank", "Is Laundering"]:
        pat[col] = pat[col].astype(np.int64)
    for col in ["Amount Received", "Amount Paid"]:
        pat[col] = pat[col].astype(np.float64)
    return pat


def attach_pattern_labels(transactions: pd.DataFrame, patterns: pd.DataFrame) -> pd.DataFrame:
    """is_suspicious/aml_pattern come from the pattern-file match (partial, structural
    ground truth). is_laundering is the raw authoritative row label. Kept separate per
    ml_spec.md open-decision #2 — do not collapse into one column."""
    join_cols = TRANS_COLS  # exact tuple match, no shared key (phase1.md §9)
    pat_dedup = patterns.drop_duplicates(subset=join_cols)[join_cols + ["aml_pattern"]]

    merged = transactions.merge(pat_dedup, on=join_cols, how="left")
    merged["is_suspicious"] = merged["aml_pattern"].notna()
    merged["aml_pattern"] = merged["aml_pattern"].fillna("NORMAL")
    merged["is_laundering"] = merged["Is Laundering"].astype(bool)
    return merged


def enrich(transactions: pd.DataFrame) -> pd.DataFrame:
    df = transactions.copy()

    # temporal
    ts = pd.to_datetime(df["Timestamp"])
    df["hour"] = ts.dt.hour
    df["day_of_week"] = ts.dt.day_name()

    # amount_category: quantile buckets over log1p(Amount Received), not arbitrary
    # round-number edges (phase1.md §5 — heavy right-tailed distribution).
    log_amount = np.log1p(df["Amount Received"])
    df["amount_category"] = pd.qcut(
        log_amount, q=len(AMOUNT_BUCKET_LABELS), labels=AMOUNT_BUCKET_LABELS
    )

    # per-sender baseline stats, computed once (precomputed-baseline caching strategy)
    grp = df.groupby("From Account")["Amount Received"]
    baseline = grp.agg(txn_count="count", total_volume="sum", avg_amount="mean", std_amount="std")
    baseline["std_amount"] = baseline["std_amount"].fillna(0.0)
    df = df.merge(baseline, on="From Account", how="left")

    unique_receivers = df.groupby("From Account")["To Account"].nunique()
    df["unique_receivers"] = df["From Account"].map(unique_receivers)

    # deviation_from_avg: z-score vs sender's own baseline, guard div-by-zero for
    # single-transaction senders (std_amount == 0 -> deviation defined as 0, not NaN/inf)
    denom = df["std_amount"].replace(0.0, np.nan)
    df["deviation_from_avg"] = ((df["Amount Received"] - df["avg_amount"]) / denom).fillna(0.0)

    # extra columns carried from Phase 1 findings (ml_spec.md Phase 2, "also carry forward")
    df["currency_mismatch"] = df["Receiving Currency"] != df["Payment Currency"]
    df["is_self_loop"] = df["From Account"] == df["To Account"]

    # payment_format_risk: empirical P(is_laundering | format) over the full dataset,
    # a fixed aggregate encoding (not a per-row label leak) — Payment Format is the
    # strongest class-separating signal found in Phase 1 (phase1.md §6).
    format_risk = df.groupby("Payment Format")["is_laundering"].mean()
    df["payment_format_risk"] = df["Payment Format"].map(format_risk).astype(np.float64)

    return df


def join_accounts(df: pd.DataFrame, accounts: pd.DataFrame) -> pd.DataFrame:
    """Confirmed 100% overlap in Phase 1 (phase1.md §3) — safe inner-join, no fallback path."""
    acc = accounts.rename(columns={
        "Bank ID": "acc_bank_id", "Account Number": "acc_account_number",
        "Entity ID": "acc_entity_id", "Entity Name": "acc_entity_name", "Bank Name": "acc_bank_name",
    })
    sender = acc.add_prefix("sender_")
    df = df.merge(
        sender, left_on=["From Bank", "From Account"],
        right_on=["sender_acc_bank_id", "sender_acc_account_number"], how="inner",
    )
    receiver = acc.add_prefix("receiver_")
    df = df.merge(
        receiver, left_on=["To Bank", "To Account"],
        right_on=["receiver_acc_bank_id", "receiver_acc_account_number"], how="inner",
    )
    drop_cols = [
        "sender_acc_bank_id", "sender_acc_account_number",
        "receiver_acc_bank_id", "receiver_acc_account_number",
    ]
    return df.drop(columns=drop_cols)


def main():
    print("loading transactions + accounts...")
    transactions = load_transactions()
    accounts = pd.read_csv(ACCOUNTS_PATH)
    print(f"  transactions: {len(transactions):,} rows")

    print("parsing pattern file...")
    patterns = parse_patterns(PATTERNS_PATH)
    print(f"  pattern rows: {len(patterns):,}, types: {dict(Counter(patterns['aml_pattern']))}")

    print("attaching pattern labels...")
    df = attach_pattern_labels(transactions, patterns)
    print(f"  is_suspicious=True: {df['is_suspicious'].sum():,}, is_laundering=True: {df['is_laundering'].sum():,}")

    print("enriching (temporal, amount buckets, baselines, extras)...")
    df = enrich(df)

    print("joining accounts (sender + receiver)...")
    before = len(df)
    df = join_accounts(df, accounts)
    print(f"  rows before join: {before:,}, after: {len(df):,}")

    # One file, not two. spec.md names both HI-Small_Enriched.parquet and
    # HI-Small_Agent_Ready.parquet as deliverables, but Phase 3 adds no precomputed columns of
    # its own ("anything genuinely query-time-only... doesn't belong precomputed"), so the
    # second file was a byte-identical 352 MB copy of the first. Phase 3's real deliverable is
    # the `feature_eng` tool, which computes scoped features on demand over this table.
    print(f"writing {OUT_PATH} ...")
    df.to_parquet(OUT_PATH, index=False)
    print(f"done. {len(df):,} rows, {len(df.columns)} columns.")

    return df


if __name__ == "__main__":
    main()
