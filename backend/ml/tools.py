"""Tool registry — the ML workstream's hand-off surface to the agent loop.

`ml_spec.md` ("Interface contract with teammate") requires the five ML-owned tools to be
handed over as locked signatures before the orchestration loop is wired. This module is that
hand-off: one dispatch table, one JSON-Schema description per tool, and the mapping from
`risk()` output to the `flags` table columns.

`TOOL_SCHEMAS` is plain JSON Schema, which `agent/providers.py` wraps into Gemini function
declarations at call time. Nothing here imports an SDK.

Usage from the loop:

    from ml.tools import TOOL_SCHEMAS, dispatch
    ...
    result = dispatch(tool_name, tool_args)   # always a dict, never raises
"""
from typing import Any, Callable

from ml.anomaly import anomaly
from ml.eda import (
    AGGREGATIONS,
    COLUMNS,
    INTERVALS,
    OPERATIONS,
    SOURCES,
    eda,
)
from ml.explain import explain
from ml.feature_eng import feature_eng
from ml.risk import RISK_LEVELS, risk

# --- Shared schema fragments ---------------------------------------------------------------

# Dates: the stored column is a VARCHAR (ml/dates.py), but callers may use either separator and
# may omit the time - the tools normalize. Spelled out here so the model does not have to
# guess, since guessing wrongly used to return an empty result rather than an error.
_DATE_DESC = (
    "Date as YYYY-MM-DD or YYYY/MM/DD, optionally with a HH:MM time. A bare date covers the "
    "whole day. Dataset covers 2022-09-01 to 2022-09-18."
)

_SCOPE_SCHEMA = {
    "type": "object",
    "description": "Which transactions to operate on. At least one of account_id or "
                   "date_range is required.",
    "properties": {
        "account_id": {
            "type": "string",
            "description": "Account number, e.g. '8000EBD30'.",
        },
        "role": {
            "type": "string",
            "enum": ["sender", "receiver", "both"],
            "description": "Which side of the transaction account_id must appear on. "
                           "Default 'both'.",
        },
        "date_range": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 2,
            "maxItems": 2,
            "description": f"[start, end]. {_DATE_DESC}",
        },
        "min_amount": {"type": "number", "description": "Minimum Amount Received."},
        "max_amount": {"type": "number", "description": "Maximum Amount Received."},
        "payment_format": {
            "type": "string",
            "description": "Exact payment format, e.g. 'ACH', 'Cheque', 'Credit Card'.",
        },
        "velocity_window_days": {
            "type": "integer",
            "minimum": 1,
            "description": "feature_eng only: also report transaction count in the last N "
                           "days of the scope.",
        },
        "limit": {"type": "integer", "minimum": 1, "description": "Max rows to return."},
    },
}

_TXN_COLUMN_NAMES = sorted(COLUMNS["transactions"])
_RULE_HIT_COLUMN_NAMES = sorted(COLUMNS["rule_hits"])

_FILTER_SCHEMA = {
    "type": "array",
    "description": "Filters combined with AND.",
    "items": {
        "type": "object",
        "required": ["column"],
        "properties": {
            "column": {
                "type": "string",
                "description": "For source 'transactions' one of: "
                               + ", ".join(_TXN_COLUMN_NAMES)
                               + ". For source 'rule_hits' one of: "
                               + ", ".join(_RULE_HIT_COLUMN_NAMES) + ".",
            },
            "op": {
                "type": "string",
                "enum": ["=", "!=", ">", ">=", "<", "<=", "in", "between",
                         "contains", "is_null", "not_null"],
                "description": "Default '='. On the timestamp column a bare date means the "
                               "whole day.",
            },
            "value": {
                "description": "Scalar for comparison ops, list for 'in', [low, high] for "
                               f"'between'. {_DATE_DESC}",
            },
        },
    },
}

# --- Schemas -------------------------------------------------------------------------------

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "eda",
        "description": (
            "Ad-hoc aggregate queries over the transaction dataset: counts, group-bys, "
            "distributions, time series, top accounts, and row samples. Use for open-ended "
            "questions about the data ('how many ACH transactions were flagged', 'busiest "
            "day'). Not for scoring a specific account - use anomaly for that."
        ),
        "input_schema": {
            "type": "object",
            "required": ["operation"],
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": list(OPERATIONS),
                    "description": "count: row count. count_groups: how many groups clear a "
                                   "threshold, e.g. how many customers made 10+ transactions "
                                   "(set dimension, aggregation=count, min_value=10) - use this "
                                   "for 'how many customers/accounts', never the length of a "
                                   "ranking. aggregate: one number over a measure. "
                                   "group: measure by dimension. distribution: row counts by "
                                   "dimension. time_series: measure bucketed by time. "
                                   "top_accounts: highest-ranking accounts. sample: raw rows.",
                },
                "source": {
                    "type": "string",
                    "enum": sorted(SOURCES),
                    "description": "'transactions' (default) or 'rule_hits' (precomputed "
                                   "graph-motif hits per account).",
                },
                "filters": _FILTER_SCHEMA,
                "measure": {
                    "type": "string",
                    "description": "Column to aggregate. Required unless aggregation is "
                                   "'count'.",
                },
                "aggregation": {
                    "type": "string",
                    "enum": sorted(AGGREGATIONS),
                    "description": "Default 'count' (or 'sum' for operation 'aggregate').",
                },
                "dimension": {
                    "type": "string",
                    "description": "Column to group by. Required for group/distribution.",
                },
                "interval": {
                    "type": "string",
                    "enum": sorted(INTERVALS),
                    "description": "time_series bucket size. Default 'day'.",
                },
                "side": {
                    "type": "string",
                    "enum": ["sender", "receiver"],
                    "description": "top_accounts: rank senders or receivers. Default 'sender'.",
                },
                "columns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "sample: which columns to return.",
                },
                "min_value": {
                    "type": "number",
                    "description": "group/distribution/top_accounts: keep only groups whose "
                                   "aggregated value is at least this (SQL HAVING). Use for "
                                   "'customers with 10 or more transactions' — set "
                                   "aggregation=count and min_value=10. Without it you get a "
                                   "ranking, which is a different question.",
                },
                "order": {"type": "string", "enum": ["asc", "desc"]},
                "limit": {"type": "integer", "minimum": 1, "description": "Default 50, max 1000."},
            },
        },
    },
    {
        "name": "feature_eng",
        "description": (
            "Return the enriched transactions for a scope, plus summary aggregates and an "
            "optional velocity figure. Use to inspect what an account actually did before or "
            "after scoring it."
        ),
        "input_schema": {
            "type": "object",
            "required": ["scope"],
            "properties": {"scope": _SCOPE_SCHEMA},
        },
    },
    {
        "name": "anomaly",
        "description": (
            "Score a scope for anomalous activity. Returns a calibrated 0-1 anomaly score "
            "from the trained detectors plus any named graph-motif rule hits (FAN-OUT, CYCLE, "
            "SCATTER-GATHER, ...) for the accounts involved. This is the tool to call when "
            "asked whether an account or period is suspicious."
        ),
        "input_schema": {
            "type": "object",
            "required": ["scope"],
            "properties": {
                "scope": _SCOPE_SCHEMA,
                "method": {
                    "type": "string",
                    "enum": ["all", "isolation_forest", "lof", "zscore"],
                    "description": "Default 'all', a lift-weighted blend of the three.",
                },
            },
        },
    },
    {
        "name": "risk",
        "description": (
            "Turn an anomaly result into a discrete risk level and a recommended escalation "
            "action, plus the amount and timestamp of the transaction that drove it. Call with "
            "the full, unmodified output of anomaly."
        ),
        "input_schema": {
            "type": "object",
            "required": ["anomaly_result"],
            "properties": {
                "anomaly_result": {
                    "type": "object",
                    "description": "The dict returned by the anomaly tool, passed through "
                                   "unchanged. Do not summarise or rebuild it.",
                },
                "context": {
                    "type": "object",
                    "description": "Optional overrides, e.g. {'customer_id': '8000EBD30'}.",
                },
                "scope": {
                    "type": "object",
                    "description": "Fallback only: the same scope you passed to anomaly. Used "
                                   "to re-derive the result if anomaly_result is incomplete.",
                },
            },
        },
    },
    {
        "name": "explain",
        "description": (
            "Produce the plain-English justification for a risk result, citing the rule or "
            "signal that fired and the values that triggered it. Call with the full, "
            "unmodified output of risk."
        ),
        "input_schema": {
            "type": "object",
            "required": ["risk_result"],
            "properties": {
                "risk_result": {
                    "type": "object",
                    "description": "The dict returned by the risk tool, passed through "
                                   "unchanged.",
                },
            },
        },
    },
]

# --- Dispatch ------------------------------------------------------------------------------

TOOLS: dict[str, Callable[..., dict]] = {
    "eda": lambda **kw: eda(kw.get("query_spec", kw)),
    "feature_eng": lambda **kw: feature_eng(kw.get("scope", {})),
    "anomaly": lambda **kw: anomaly(kw.get("scope", {}), kw.get("method", "all")),
    "risk": lambda **kw: risk(
        kw.get("anomaly_result"), kw.get("context"), scope=kw.get("scope")
    ),
    "explain": lambda **kw: explain(kw.get("risk_result", {})),
}


_ALLOWED_ARGS: dict[str, set[str]] = {
    schema["name"]: set(schema["input_schema"]["properties"])
    for schema in TOOL_SCHEMAS
}
# `eda` is declared with the query spec flattened into the argument object, but models
# routinely wrap it under the parameter name instead. Both are accepted.
_ALLOWED_ARGS["eda"].add("query_spec")


def dispatch(name: str, args: dict | None = None) -> dict:
    """Call a tool by name with the model's argument dict. Always returns a dict.

    Mirrors the per-tool contract at the registry level: an unknown tool name or a malformed
    argument dict comes back as a structured error the loop can feed straight back to the
    model as a tool result, never as an exception it has to catch.
    """
    if name not in TOOLS:
        return {"error": f"unknown tool '{name}'; expected one of: {', '.join(TOOLS)}"}
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return {"error": f"arguments for '{name}' must be an object, got {type(args).__name__}"}
    if not all(isinstance(k, str) for k in args):
        return {"error": f"argument keys for '{name}' must be strings"}
    # An argument the schema does not declare means the model invented one, and silently
    # dropping it would run a query that is not the one it asked for.
    unknown = sorted(set(args) - _ALLOWED_ARGS[name])
    if unknown:
        return {
            "error": f"unknown argument(s) for '{name}': {', '.join(unknown)}; "
                     f"expected: {', '.join(sorted(_ALLOWED_ARGS[name]))}"
        }
    try:
        return TOOLS[name](**args)
    except TypeError as exc:
        return {"error": f"bad arguments for '{name}': {exc}"}


# --- flags-table mapping ---------------------------------------------------------------------

# Maps the `flags` columns (spec.md "Database Schema") onto keys of the `risk()` result, so the
# persistence layer does not have to guess. Everything not listed here is teammate-owned:
# `id` and `query_id` are assigned at insert time, and `escalated_at` stays NULL until a judge
# clicks escalate.
#
# Notes for whoever writes the INSERT:
#   * `explanation` is NOT in the risk result - it comes from calling explain(risk_result) and
#     reading its "explanation" key.
#   * `transaction_id` is TEXT, not an integer. The Kaggle data has no transaction key
#     (phase1.md §9), so it is a deterministic hash of the identifying field tuple.
#   * `risk_level` is TEXT, one of RISK_LEVELS below.
#   * `anomaly_score` is the raw detector score in [0,1]. The blended rule+detector figure the
#     UI should sort on is `risk_score`, which has no column in the current schema - either add
#     one or sort by risk_level then anomaly_score.
FLAGS_COLUMN_MAP: dict[str, str] = {
    "customer_id": "customer_id",
    "transaction_id": "transaction_id",
    "risk_level": "risk_level",
    "pattern_detected": "pattern_detected",
    "anomaly_score": "anomaly_score",
    "escalation_action": "escalation_action",
    "explanation": "<from explain(risk_result)['explanation']>",
}

# Not persisted by the current schema, but returned by risk() and worth surfacing in the
# transparency panel: risk_score (blended 0-1), contributing_signals (per-rule breakdown,
# counterparty hits, row flag fractions, and whether the scope was truncated).
FLAGS_EXTRA_FIELDS: tuple[str, ...] = ("risk_score", "contributing_signals")

VALID_RISK_LEVELS: tuple[str, ...] = tuple(RISK_LEVELS)
