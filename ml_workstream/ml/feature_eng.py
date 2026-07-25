"""
Phase 3 — feature_eng tool. Computes scoped features on demand (e.g. "features for
account 8000EBD30") against the Phase 2 enriched Parquet, via DuckDB.

Contract (ml_spec.md "Interface contract with teammate"):
- plain dict in, plain dict out — no custom classes, no DataFrame crossing the boundary
- never raises on bad input — returns {"error": ...} instead
- pure / side-effect-free (read-only over the enriched Parquet)
"""
from typing import Any

from ml.cache import cached_tool
from ml.data import get_connection
from ml.dates import normalize_range

MAX_LIMIT = 1000
DEFAULT_LIMIT = 200

_RECORD_COLUMNS = [
    "Timestamp", "From Account", "To Account", "From Bank", "To Bank",
    "Amount Received", "Receiving Currency", "Payment Format",
    "is_laundering", "is_suspicious", "aml_pattern", "amount_category",
    "deviation_from_avg", "currency_mismatch", "is_self_loop", "payment_format_risk",
]


def validate_scope(scope: dict) -> tuple[dict, list[str]]:
    """Normalize + validate scope fields. Returns (clean_scope, errors) — never raises."""
    if not isinstance(scope, dict):
        return {}, ["scope must be a dict"]

    errors: list[str] = []
    clean: dict[str, Any] = {}

    if "account_id" in scope:
        if not isinstance(scope["account_id"], str) or not scope["account_id"]:
            errors.append("account_id must be a non-empty string")
        else:
            clean["account_id"] = scope["account_id"]

    role = scope.get("role", "both")
    if role not in ("sender", "receiver", "both"):
        errors.append("role must be one of: sender, receiver, both")
    else:
        clean["role"] = role

    if "date_range" in scope:
        # Normalized to the stored VARCHAR format here, once, so `build_where` can compare
        # directly. Passing a caller's ISO date straight into SQL matched zero rows and
        # reported no error - see ml/dates.py.
        normalized, err = normalize_range(scope["date_range"])
        if err:
            errors.append(f"date_range: {err}")
        else:
            clean["date_range"] = normalized

    for key in ("min_amount", "max_amount"):
        if key in scope:
            val = scope[key]
            if not isinstance(val, (int, float)) or isinstance(val, bool):
                errors.append(f"{key} must be numeric")
            else:
                clean[key] = float(val)

    if "min_amount" in clean and "max_amount" in clean and clean["min_amount"] > clean["max_amount"]:
        errors.append("min_amount must be <= max_amount")

    if "payment_format" in scope:
        if not isinstance(scope["payment_format"], str):
            errors.append("payment_format must be a string")
        else:
            clean["payment_format"] = scope["payment_format"]

    if "velocity_window_days" in scope:
        val = scope["velocity_window_days"]
        if not isinstance(val, int) or isinstance(val, bool) or val <= 0:
            errors.append("velocity_window_days must be a positive integer")
        else:
            clean["velocity_window_days"] = val

    if "limit" in scope:
        val = scope["limit"]
        if not isinstance(val, int) or isinstance(val, bool) or val <= 0:
            errors.append("limit must be a positive integer")
        else:
            clean["limit"] = min(val, MAX_LIMIT)

    # Keyed off what was supplied, not what survived validation: a malformed date_range
    # already reports why it was rejected, and adding "you must supply a date_range" on top
    # of that reads as if it were missing entirely.
    if "account_id" not in scope and "date_range" not in scope:
        errors.append("scope must include at least account_id or date_range")

    return clean, errors


def build_where(scope: dict) -> tuple[str, list]:
    clauses = []
    params: list = []

    if "account_id" in scope:
        role = scope["role"]
        if role == "sender":
            clauses.append('"From Account" = ?')
            params.append(scope["account_id"])
        elif role == "receiver":
            clauses.append('"To Account" = ?')
            params.append(scope["account_id"])
        else:
            clauses.append('("From Account" = ? OR "To Account" = ?)')
            params.extend([scope["account_id"], scope["account_id"]])

    if "date_range" in scope:
        clauses.append('"Timestamp" >= ? AND "Timestamp" <= ?')
        params.extend(scope["date_range"])

    if "min_amount" in scope:
        clauses.append('"Amount Received" >= ?')
        params.append(scope["min_amount"])

    if "max_amount" in scope:
        clauses.append('"Amount Received" <= ?')
        params.append(scope["max_amount"])

    if "payment_format" in scope:
        clauses.append('"Payment Format" = ?')
        params.append(scope["payment_format"])

    where_sql = " AND ".join(clauses) if clauses else "TRUE"
    return where_sql, params


@cached_tool()
def feature_eng(scope: dict) -> dict:
    clean_scope, errors = validate_scope(scope)
    if errors:
        return {"error": "; ".join(errors), "scope": scope}

    try:
        con = get_connection()
        where_sql, params = build_where(clean_scope)

        agg_sql = f"""
            SELECT
                count(*) AS txn_count,
                coalesce(sum("Amount Received"), 0) AS total_volume,
                coalesce(avg("Amount Received"), 0) AS avg_amount,
                coalesce(stddev("Amount Received"), 0) AS std_amount,
                count(DISTINCT "To Account") AS unique_receivers,
                count(DISTINCT "From Account") AS unique_senders,
                min("Timestamp") AS earliest_ts,
                max("Timestamp") AS latest_ts,
                sum(CASE WHEN is_suspicious THEN 1 ELSE 0 END) AS suspicious_count,
                sum(CASE WHEN is_laundering THEN 1 ELSE 0 END) AS laundering_count
            FROM enriched WHERE {where_sql}
        """
        agg_row = con.execute(agg_sql, params).fetchone()
        agg_cols = [d[0] for d in con.description]
        aggregate = dict(zip(agg_cols, agg_row))

        limit = clean_scope.get("limit", DEFAULT_LIMIT)
        cols_sql = ", ".join(f'"{c}"' for c in _RECORD_COLUMNS)
        rec_sql = (
            f'SELECT {cols_sql} FROM enriched WHERE {where_sql} '
            f'ORDER BY "Timestamp" DESC LIMIT ?'
        )
        rec_rows = con.execute(rec_sql, params + [limit]).fetchall()
        rec_cols = [d[0] for d in con.description]
        records = [dict(zip(rec_cols, row)) for row in rec_rows]

        result = {
            "scope": clean_scope,
            "aggregate": aggregate,
            "records": records,
            "record_count_returned": len(records),
            "record_count_truncated": aggregate["txn_count"] > len(records),
        }

        if "velocity_window_days" in clean_scope and "account_id" in clean_scope:
            window = clean_scope["velocity_window_days"]
            ts_expr = "strptime(\"Timestamp\", '%Y/%m/%d %H:%M')"
            vel_sql = f"""
                SELECT count(*) AS txn_count_in_window
                FROM enriched
                WHERE {where_sql}
                  AND {ts_expr} >= (
                      SELECT max({ts_expr}) - INTERVAL '{window} days'
                      FROM enriched WHERE {where_sql}
                  )
            """
            vel_row = con.execute(vel_sql, params + params).fetchone()
            result["velocity"] = {
                "window_days": window,
                "txn_count_in_window": vel_row[0],
                "rate_per_day": vel_row[0] / window,
            }

        return result
    except Exception as exc:
        return {"error": str(exc), "scope": scope}
