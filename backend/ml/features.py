"""
Feature spec shared by model training (scripts/train_models.py) and scoring (ml/anomaly.py).

Single source of truth on purpose: an Isolation Forest scored on columns in a different order,
or with a category encoded differently than at fit time, returns confident nonsense rather
than an error. Both sides call FEATURE_SQL and get the same matrix.
"""

# Ordered categories from phase2.md §2 decision #4 (quantile buckets over log1p amount).
AMOUNT_CATEGORY_ORDER = ["micro", "small", "medium", "large", "xlarge", "xxlarge"]
DAY_ORDER = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# Order is load-bearing — this is the column order of the fitted matrix.
FEATURE_COLUMNS = [
    "log_amount",
    "amount_spread",
    "hour",
    "day_index",
    "amount_category_index",
    "deviation_from_avg",
    "log_txn_count",
    "log_total_volume",
    "log_avg_amount",
    "log_std_amount",
    "log_unique_receivers",
    "currency_mismatch_i",
    "is_self_loop_i",
    "payment_format_risk",
]


def _case(col: str, order: list[str]) -> str:
    whens = " ".join(f"WHEN '{v}' THEN {i}" for i, v in enumerate(order))
    return f"CASE {col} {whens} ELSE -1 END"


# Heavy right tail on every volume/count column (phase1.md §5) — log1p keeps a single
# 14,230-receiver hub from dominating the distance geometry both detectors rely on.
FEATURE_SQL = f"""
    ln(1 + "Amount Received")                        AS log_amount,
    "Amount Paid" - "Amount Received"                AS amount_spread,
    hour                                             AS hour,
    {_case('day_of_week', DAY_ORDER)}                AS day_index,
    {_case('amount_category', AMOUNT_CATEGORY_ORDER)} AS amount_category_index,
    deviation_from_avg                               AS deviation_from_avg,
    ln(1 + txn_count)                                AS log_txn_count,
    ln(1 + total_volume)                             AS log_total_volume,
    ln(1 + avg_amount)                               AS log_avg_amount,
    ln(1 + std_amount)                               AS log_std_amount,
    ln(1 + unique_receivers)                         AS log_unique_receivers,
    CAST(currency_mismatch AS INTEGER)               AS currency_mismatch_i,
    CAST(is_self_loop AS INTEGER)                    AS is_self_loop_i,
    payment_format_risk                              AS payment_format_risk
"""

# Note on payment_format_risk: it is P(is_laundering | Payment Format) computed over the whole
# dataset at enrichment time (phase2.md §4). As a fixed 7-value aggregate encoding it does not
# leak a row's own label, but it does carry label information in aggregate — which is why the
# detectors below are evaluated against held-out labels rather than trusted on training fit.
