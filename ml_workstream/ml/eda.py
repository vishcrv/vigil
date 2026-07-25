"""
Phase 7 — `eda(query_spec) -> dict`.

The runtime ad-hoc query tool the agent calls for open-ended questions ("how many laundering
transactions used ACH", "busiest day of the week"), executed via DuckDB over the enriched
Parquet.

**Not** raw SQL from the LLM. `ml_spec.md` Phase 7 calls that an injection risk even in a local
single-user tool, and it is right: the model that writes the query is steered by whatever text
the query itself came from. This module exposes a fixed catalogue of query *shapes* instead.

The security model has exactly two rules, and they cover the whole surface:

1. **Every literal is bound** as a DuckDB `?` parameter. Values never reach the SQL string.
2. **Every identifier is allow-listed.** Column names, aggregate functions, sort direction and
   interval cannot be parameterized in SQL, so each is looked up in a fixed dict here and the
   *stored* value is what gets interpolated — caller input is only ever a lookup key. A
   caller-supplied string never lands in a query, quoted or otherwise.

Anything failing either rule returns a structured error rather than executing.
"""
from typing import Any

from ml.cache import cached_tool
from ml.data import get_connection
from ml.dates import normalize_bound, normalize_prefix, normalize_range

MAX_LIMIT = 1000
DEFAULT_LIMIT = 50

SOURCES = {"transactions": "enriched", "rule_hits": "rule_hits"}

# --- Allow-lists ---------------------------------------------------------------------------
# Key = name the caller may use. Value = the real column, interpolated only after lookup.

_TXN_COLUMNS = {
    "timestamp": '"Timestamp"',
    "from_bank": '"From Bank"',
    "from_account": '"From Account"',
    "to_bank": '"To Bank"',
    "to_account": '"To Account"',
    "amount_received": '"Amount Received"',
    "amount_paid": '"Amount Paid"',
    "receiving_currency": '"Receiving Currency"',
    "payment_currency": '"Payment Currency"',
    "payment_format": '"Payment Format"',
    "is_laundering": "is_laundering",
    "is_suspicious": "is_suspicious",
    "aml_pattern": "aml_pattern",
    "hour": "hour",
    "day_of_week": "day_of_week",
    "amount_category": "amount_category",
    "txn_count": "txn_count",
    "total_volume": "total_volume",
    "avg_amount": "avg_amount",
    "std_amount": "std_amount",
    "unique_receivers": "unique_receivers",
    "deviation_from_avg": "deviation_from_avg",
    "currency_mismatch": "currency_mismatch",
    "is_self_loop": "is_self_loop",
    "payment_format_risk": "payment_format_risk",
    "sender_bank": "sender_acc_bank_name",
    "sender_entity": "sender_acc_entity_name",
    "receiver_bank": "receiver_acc_bank_name",
    "receiver_entity": "receiver_acc_entity_name",
}

_RULE_HIT_COLUMNS = {
    "account": "account",
    "rule": "rule",
    "score": "score",
    "evidence": "evidence",
}

COLUMNS: dict[str, dict[str, str]] = {
    "transactions": _TXN_COLUMNS,
    "rule_hits": _RULE_HIT_COLUMNS,
}

# Numeric columns only — averaging a bank name is a caller mistake worth reporting, not
# something to let DuckDB fail on later with a less clear message.
MEASURES: dict[str, set[str]] = {
    "transactions": {
        "amount_received", "amount_paid", "txn_count", "total_volume", "avg_amount",
        "std_amount", "unique_receivers", "deviation_from_avg", "payment_format_risk",
        "hour",
    },
    "rule_hits": {"score"},
}

AGGREGATIONS = {
    "sum": "sum", "avg": "avg", "min": "min", "max": "max",
    "median": "median", "stddev": "stddev", "count": "count",
    "count_distinct": "count(DISTINCT {col})",
}

COMPARISON_OPS = {"=": "=", "!=": "!=", ">": ">", ">=": ">=", "<": "<", "<=": "<="}

ORDER_DIRECTIONS = {"asc": "ASC", "desc": "DESC"}

# Timestamp is VARCHAR in fixed-width "YYYY/MM/DD HH:MM" form (phase3.md §3a), so a prefix
# substring buckets it correctly without parsing.
INTERVALS = {"day": 10, "hour": 13, "month": 7}

OPERATIONS = ("count", "aggregate", "group", "distribution", "time_series",
              "top_accounts", "sample")

DEFAULT_SAMPLE_COLUMNS = [
    "timestamp", "from_account", "to_account", "amount_received",
    "payment_format", "aml_pattern", "is_laundering",
]


def _resolve_column(source: str, name: Any, errors: list[str], role: str) -> str | None:
    table = COLUMNS[source]
    if not isinstance(name, str) or name not in table:
        errors.append(
            f"{role} must be one of the allowed columns for source '{source}': "
            f"{', '.join(sorted(table))}"
        )
        return None
    return table[name]


def _normalize_ts(op: str, value: Any) -> tuple[Any, str | None]:
    """Rewrite a timestamp filter value into the stored `YYYY/MM/DD HH:MM` format.

    Handles every op except `=`/`!=`, which the caller turns into a day range instead.
    """
    if op in COMPARISON_OPS:
        return normalize_bound(value, upper=op in ("<", "<="))
    if op == "between":
        return normalize_range(value)
    if op == "in":
        if not isinstance(value, list) or not value:
            return None, "must be a non-empty list for op 'in'"
        bounds = []
        for entry in value:
            bound, err = normalize_bound(entry)
            if err:
                return None, err
            bounds.append(bound)
        return bounds, None
    if op == "contains":
        return normalize_prefix(value)
    # is_null / not_null ignore the value entirely; an unknown op is reported downstream.
    return value, None


def _build_filters(source: str, raw: Any, errors: list[str]) -> tuple[str, list, list[dict]]:
    """Translate the filter list into a parameterized WHERE clause.

    Column names are resolved through the allow-list; every value is appended to `params` and
    referenced as `?`. No caller-supplied text is ever concatenated into the SQL.
    """
    if raw is None:
        return "TRUE", [], []
    if not isinstance(raw, list):
        errors.append("filters must be a list")
        return "TRUE", [], []

    clauses: list[str] = []
    params: list = []
    normalized: list[dict] = []

    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            errors.append(f"filter[{i}] must be a dict")
            continue
        col = _resolve_column(source, item.get("column"), errors, f"filter[{i}].column")
        if col is None:
            continue
        op = item.get("op", "=")
        value = item.get("value")

        # `Timestamp` is a lexicographically-compared VARCHAR, so a caller's ISO date has to
        # be rewritten into the stored format before it is bound - otherwise it matches
        # nothing and reports success. See ml/dates.py.
        if item.get("column") == "timestamp" and source == "transactions":
            if op in ("=", "!="):
                # A bare date means the whole day, not the single instant at midnight, so
                # equality on a date becomes a range over it.
                pair, err = normalize_range([value, value])
                if err:
                    errors.append(f"filter[{i}].value: {err}")
                    continue
                clauses.append(f"{col} {'NOT ' if op == '!=' else ''}BETWEEN ? AND ?")
                params.extend(pair)
                normalized.append({"column": "timestamp", "op": op, "value": value})
                continue

            value, err = _normalize_ts(op, value)
            if err:
                errors.append(f"filter[{i}].value: {err}")
                continue

        if op in COMPARISON_OPS:
            clauses.append(f"{col} {COMPARISON_OPS[op]} ?")
            params.append(value)
        elif op == "in":
            if not isinstance(value, list) or not value:
                errors.append(f"filter[{i}].value must be a non-empty list for op 'in'")
                continue
            if len(value) > 100:
                errors.append(f"filter[{i}] 'in' list capped at 100 values")
                continue
            clauses.append(f"{col} IN ({', '.join('?' for _ in value)})")
            params.extend(value)
        elif op == "between":
            if not isinstance(value, (list, tuple)) or len(value) != 2:
                errors.append(f"filter[{i}].value must be a [low, high] pair for op 'between'")
                continue
            clauses.append(f"{col} BETWEEN ? AND ?")
            params.extend(value)
        elif op == "contains":
            if not isinstance(value, str):
                errors.append(f"filter[{i}].value must be a string for op 'contains'")
                continue
            # The wildcards are ours; the caller's text is still a bound parameter, so a value
            # of "%' OR 1=1 --" is matched as a literal substring, not parsed as SQL.
            clauses.append(f"{col} LIKE ?")
            params.append(f"%{value}%")
        elif op == "is_null":
            clauses.append(f"{col} IS NULL")
        elif op == "not_null":
            clauses.append(f"{col} IS NOT NULL")
        else:
            errors.append(
                f"filter[{i}].op must be one of: "
                f"{', '.join(sorted(set(COMPARISON_OPS) | {'in', 'between', 'contains', 'is_null', 'not_null'}))}"
            )
            continue
        # Echoes what the caller asked for, not the rewritten bound - the rewritten form is
        # visible in `sql_parameters` for anyone checking what actually ran.
        normalized.append({"column": item.get("column"), "op": op, "value": item.get("value")})

    return (" AND ".join(clauses) if clauses else "TRUE"), params, normalized


def _agg_expr(aggregation: str, col: str) -> str:
    template = AGGREGATIONS[aggregation]
    return template.format(col=col) if "{col}" in template else f"{template}({col})"


def _limit(spec: dict, errors: list[str]) -> int:
    raw = spec.get("limit", DEFAULT_LIMIT)
    if not isinstance(raw, int) or isinstance(raw, bool) or raw <= 0:
        errors.append("limit must be a positive integer")
        return DEFAULT_LIMIT
    return min(raw, MAX_LIMIT)


@cached_tool()
def eda(query_spec: dict) -> dict:
    if not isinstance(query_spec, dict):
        return {"error": "query_spec must be a dict"}

    errors: list[str] = []
    operation = query_spec.get("operation")
    if operation not in OPERATIONS:
        return {
            "error": f"operation must be one of: {', '.join(OPERATIONS)}",
            "query_spec": query_spec,
        }

    source = query_spec.get("source", "transactions")
    if source not in SOURCES:
        return {
            "error": f"source must be one of: {', '.join(sorted(SOURCES))}",
            "query_spec": query_spec,
        }

    try:
        where_sql, params, filters_norm = _build_filters(source, query_spec.get("filters"), errors)
        limit = _limit(query_spec, errors)
        table = SOURCES[source]

        order = query_spec.get("order", "desc")
        if order not in ORDER_DIRECTIONS:
            errors.append(f"order must be one of: {', '.join(sorted(ORDER_DIRECTIONS))}")
        order_sql = ORDER_DIRECTIONS.get(order, "DESC")

        sql = None
        exec_params: list = list(params)

        if operation == "count":
            sql = f"SELECT count(*) AS n FROM {table} WHERE {where_sql}"

        elif operation == "aggregate":
            measure = query_spec.get("measure")
            aggregation = query_spec.get("aggregation", "sum")
            col = _resolve_column(source, measure, errors, "measure")
            if aggregation not in AGGREGATIONS:
                errors.append(f"aggregation must be one of: {', '.join(sorted(AGGREGATIONS))}")
            elif col and aggregation not in ("count", "count_distinct") \
                    and measure not in MEASURES[source]:
                errors.append(f"measure '{measure}' is not numeric; use count or count_distinct")
            if not errors:
                sql = (f"SELECT {_agg_expr(aggregation, col)} AS value "
                       f"FROM {table} WHERE {where_sql}")

        elif operation in ("group", "distribution"):
            dimension = query_spec.get("dimension")
            dim_col = _resolve_column(source, dimension, errors, "dimension")
            if operation == "distribution":
                measure, aggregation, measure_col = None, "count", "*"
            else:
                measure = query_spec.get("measure")
                aggregation = query_spec.get("aggregation", "count")
                if aggregation not in AGGREGATIONS:
                    errors.append(
                        f"aggregation must be one of: {', '.join(sorted(AGGREGATIONS))}")
                if aggregation == "count":
                    measure_col = "*"
                else:
                    measure_col = _resolve_column(source, measure, errors, "measure")
                    if measure_col and aggregation != "count_distinct" \
                            and measure not in MEASURES[source]:
                        errors.append(f"measure '{measure}' is not numeric")
            if not errors and dim_col:
                sql = (
                    f"SELECT {dim_col} AS dimension, "
                    f"{_agg_expr(aggregation, measure_col)} AS value "
                    f"FROM {table} WHERE {where_sql} "
                    f"GROUP BY {dim_col} ORDER BY value {order_sql} LIMIT ?"
                )
                exec_params = params + [limit]

        elif operation == "time_series":
            if source != "transactions":
                errors.append("time_series is only available for source 'transactions'")
            interval = query_spec.get("interval", "day")
            if interval not in INTERVALS:
                errors.append(f"interval must be one of: {', '.join(sorted(INTERVALS))}")
            measure = query_spec.get("measure")
            aggregation = query_spec.get("aggregation", "count")
            if aggregation not in AGGREGATIONS:
                errors.append(f"aggregation must be one of: {', '.join(sorted(AGGREGATIONS))}")
            if aggregation == "count":
                measure_col = "*"
            else:
                measure_col = _resolve_column(source, measure, errors, "measure")
                if measure_col and aggregation != "count_distinct" \
                        and measure not in MEASURES[source]:
                    errors.append(f"measure '{measure}' is not numeric")
            if not errors:
                width = INTERVALS[interval]
                sql = (
                    f'SELECT substr("Timestamp", 1, {width}) AS bucket, '
                    f"{_agg_expr(aggregation, measure_col)} AS value "
                    f"FROM {table} WHERE {where_sql} "
                    f"GROUP BY bucket ORDER BY bucket ASC LIMIT ?"
                )
                exec_params = params + [limit]

        elif operation == "top_accounts":
            if source != "transactions":
                errors.append("top_accounts is only available for source 'transactions'")
            side = query_spec.get("side", "sender")
            if side not in ("sender", "receiver"):
                errors.append("side must be 'sender' or 'receiver'")
            aggregation = query_spec.get("aggregation", "count")
            measure = query_spec.get("measure", "amount_received")
            if aggregation not in AGGREGATIONS:
                errors.append(f"aggregation must be one of: {', '.join(sorted(AGGREGATIONS))}")
            if aggregation == "count":
                measure_col = "*"
            else:
                measure_col = _resolve_column(source, measure, errors, "measure")
                if measure_col and aggregation != "count_distinct" \
                        and measure not in MEASURES[source]:
                    errors.append(f"measure '{measure}' is not numeric")
            if not errors:
                acct_col = '"From Account"' if side == "sender" else '"To Account"'
                sql = (
                    f"SELECT {acct_col} AS account, "
                    f"{_agg_expr(aggregation, measure_col)} AS value "
                    f"FROM {table} WHERE {where_sql} "
                    f"GROUP BY {acct_col} ORDER BY value {order_sql} LIMIT ?"
                )
                exec_params = params + [limit]

        elif operation == "sample":
            requested = query_spec.get("columns") or (
                DEFAULT_SAMPLE_COLUMNS if source == "transactions"
                else sorted(_RULE_HIT_COLUMNS)
            )
            if not isinstance(requested, list) or not requested:
                errors.append("columns must be a non-empty list when provided")
                requested = []
            resolved = [
                _resolve_column(source, c, errors, "columns[]") for c in requested
            ]
            if not errors:
                projection = ", ".join(
                    f"{col} AS {name}" for col, name in zip(resolved, requested)
                )
                sql = f"SELECT {projection} FROM {table} WHERE {where_sql} LIMIT ?"
                exec_params = params + [limit]

        if errors:
            return {"error": "; ".join(errors), "query_spec": query_spec}
        if sql is None:
            return {"error": "could not build a query from this spec", "query_spec": query_spec}

        con = get_connection()
        if source == "rule_hits":
            _ensure_rule_hits_view(con)

        rows = con.execute(sql, exec_params).fetchall()
        columns = [d[0] for d in con.description]
        records = [dict(zip(columns, row)) for row in rows]

        return {
            "operation": operation,
            "source": source,
            "query_spec": {
                "operation": operation, "source": source, "filters": filters_norm,
                **{k: v for k, v in query_spec.items()
                   if k in ("measure", "aggregation", "dimension", "interval", "side",
                            "order", "columns")},
                "limit": limit,
            },
            "columns": columns,
            "records": records,
            "row_count": len(records),
            # Returned for the execution-summary panel spec.md calls the key differentiator:
            # the judge can see exactly what ran. Placeholders are shown unfilled because the
            # values were bound, not interpolated.
            "sql": " ".join(sql.split()),
            "sql_parameters": exec_params,
        }
    except Exception as exc:
        return {"error": str(exc), "query_spec": query_spec}


def _ensure_rule_hits_view(con) -> None:
    """Register the Phase 4 rule-hits parquet as a view on first use."""
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "data" / "HI-Small_Rule_Hits.parquet"
    if not path.exists():
        raise FileNotFoundError(
            "rule hits not found - run scripts/build_rule_hits.py first"
        )
    con.execute(
        f"CREATE OR REPLACE VIEW rule_hits AS SELECT * FROM read_parquet('{path.as_posix()}')"
    )
