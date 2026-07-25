"""Hand-rolled tool-calling loop. Phase 3a: real loop, stubbed ML tools.

The five ML tools are stubs returning fixed plausible dicts in the shapes the ML owner agreed
(docs/IMPLEMENTATION_PLAN.md Phase 2). Phase 3b swaps STUBS for the real `backend/tools/*`
functions; nothing else here should need to change.
"""

import json
import math

from pydantic import ValidationError
from schemas import AgentResult, ExecutionSummary, FlaggedItem, SkippedTool

MAX_ITERATIONS = 8

# Cap on one tool result's serialised size before it goes into the context. A real `eda` over
# ~5M rows can return far more than the model can read, and an unbounded result is both a
# context-limit failure and a cost problem. Truncation is visible to the model, not silent.
MAX_TOOL_RESULT_CHARS = 20_000


# --- stubbed tools -------------------------------------------------------------------------


def eda(query_spec: dict) -> dict:
    return {
        "row_count": 5078345,
        "flagged_count": 5177,
        "total_volume": 8.42e9,
        "by_payment_format": {"ACH": 0.61, "Cheque": 0.18, "Wire": 0.12, "Other": 0.09},
        "by_hour": {"0": 21044, "9": 318902, "14": 402117, "23": 44810},
    }


def feature_eng(scope: dict) -> dict:
    return {
        "scope": scope,
        "records": [
            {
                "customer_id": "8000EBD30",
                "transaction_id": "TXN-4471902",
                "amount": 184500.0,
                "timestamp": "2022-09-01T14:22:00",
                "txn_count": 37,
                "avg_amount": 12400.0,
                "unique_receivers": 19,
                "deviation_from_avg": 6.8,
                "currency_mismatch": True,
            }
        ],
    }


def anomaly(scope: dict, method: str | None = None) -> dict:
    return {
        "anomaly_score": 0.87,
        "method_scores": {"isolation_forest": 0.91, "lof": 0.79, "z_score": 6.8},
        "rule_hits": ["FAN-OUT", "CURRENCY_MISMATCH"],
    }


def risk(anomaly_result: dict, context: dict | None = None) -> dict:
    return {
        "risk_level": "HIGH",
        "pattern_detected": "FAN-OUT",
        "anomaly_score": anomaly_result.get("anomaly_score", 0.87),
        "escalation_action": "REPORT",
    }


def explain(risk_result: dict) -> dict:
    return {
        "explanation": (
            f"Flagged as {risk_result.get('risk_level', 'HIGH')} risk: "
            f"{risk_result.get('pattern_detected', 'FAN-OUT')} pattern with an anomaly score of "
            f"{risk_result.get('anomaly_score', 0.87)}, driven by out-degree above the sender's "
            f"baseline and a payment-currency mismatch."
        )
    }


STUBS = {"eda": eda, "feature_eng": feature_eng, "anomaly": anomaly, "risk": risk, "explain": explain}

TOOL_SCHEMAS = [
    {
        "name": "eda",
        "description": "Aggregate exploration over the transaction dataset: counts, volumes, "
        "distributions. Use for dataset-wide or filtered aggregate questions.",
        "input_schema": {
            "type": "object",
            "properties": {"query_spec": {"type": "object", "description": "Filters/groupings."}},
            "required": ["query_spec"],
        },
    },
    {
        "name": "feature_eng",
        "description": "Per-entity feature records (baselines, deviation, velocity) for a scope, "
        "e.g. one account or a filtered slice. Run before anomaly detection.",
        "input_schema": {
            "type": "object",
            "properties": {"scope": {"type": "object", "description": "e.g. {'account_id': '...'}"}},
            "required": ["scope"],
        },
    },
    {
        "name": "anomaly",
        "description": "Anomaly detection (Isolation Forest, LOF, Z-score) plus a rules engine for "
        "the 8 laundering motifs. Returns anomaly_score, method_scores, rule_hits.",
        "input_schema": {
            "type": "object",
            "properties": {
                "scope": {"type": "object"},
                "method": {"type": "string", "description": "Optional: restrict to one method."},
            },
            "required": ["scope"],
        },
    },
    {
        "name": "risk",
        "description": "Map an anomaly result to risk_level, pattern_detected and a recommended "
        "escalation_action (MONITOR/REVIEW/REPORT).",
        "input_schema": {
            "type": "object",
            "properties": {
                "anomaly_result": {"type": "object"},
                "context": {"type": "object"},
            },
            "required": ["anomaly_result"],
        },
    },
    {
        "name": "explain",
        "description": "Natural-language explanation of a risk result, citing the rule/feature "
        "that fired. Run last, for anything you intend to flag.",
        "input_schema": {
            "type": "object",
            "properties": {"risk_result": {"type": "object"}},
            "required": ["risk_result"],
        },
    },
]

SYSTEM = """You are an AML (anti-money-laundering) detection agent operating over a transaction
dataset. You are the agent; the person you are talking to is a human compliance analyst using you
to investigate that dataset. Never describe the analyst as an AI or as the agent — if they ask who
they are, they are the analyst operating this tool.

Call the tools you need to answer the analyst's query — only those you need:
- Aggregate or counting questions ("how many", "which customers made N+ transactions over X")
  usually need only `eda`. A threshold or grouping question is answered by aggregation; it does
  not need ML anomaly detection.
- Single-entity suspicion checks ("is customer X suspicious") need feature_eng -> anomaly -> risk
  -> explain, scoped to that entity. They do not need `eda`.
- A pattern search already narrowed by a time window or a named motif ("structuring in the last
  30 days", "fan-out this month") should apply that filter and go straight to feature_eng ->
  anomaly -> risk -> explain on the filtered slice. Do not run a full-dataset `eda` sweep first;
  the query has already told you where to look.

If the query is not a request to analyse transaction data — a greeting, small talk, a question
about you or about the analyst, or anything otherwise out of scope — call NO tools at all, answer
directly in `summary`, and return [] for flagged_items. Never invoke a tool merely to have data to
talk about, and never quote dataset statistics in an answer that did not need them.

When you are done calling tools, reply with ONLY a JSON object (no prose, no code fences):
{
  "intent": "short label for what the query asked, e.g. 'aggregate_eda' or 'entity_risk_lookup'",
  "filters": {"any filters you applied": "value"},
  "summary": "natural-language answer for the analyst",
  "skipped": [{"name": "tool you did not call", "reason": "why it wasn't needed"}],
  "flagged_items": [
    {
      "customer_id": "...", "transaction_id": "...", "amount": 0.0,
      "timestamp": "ISO 8601 timestamp of the transaction",
      "risk_level": "from the risk tool", "pattern_detected": "from the risk tool",
      "anomaly_score": 0.0, "explanation": "from the explain tool",
      "escalation_action": "MONITOR or REVIEW or REPORT"
    }
  ]
}
Use [] for flagged_items when nothing is suspicious. Every flagged item must come from tool output."""


def _json_safe(value):
    """Coerce tool output into strictly JSON-serialisable primitives.

    The ML tools are pandas/numpy/DuckDB-backed, which leaks three things that `json.dumps`
    handles badly enough to corrupt a run — all silently, which is why this exists rather than
    trusting the "plain dicts only" contract:

      - NaN / Infinity      -> `json.dumps` emits bare `NaN`/`Infinity`, which is not valid JSON
                               and gets rejected by strict parsers upstream. Becomes null here.
      - numpy scalars       -> `default=str` turns np.int64(37) into the *string* "37", so the
                               model reasons over '37' instead of 37. `.item()` gives a real int.
      - Timestamp / NaT / DataFrame -> stringified as a last resort, but at least predictably.

    Deliberately duck-typed (`.item()`) rather than importing numpy/pandas: this module is the
    orchestration layer and shouldn't take a hard dependency on the ML stack.
    """
    if isinstance(value, (str, bytes, bool)) or value is None:
        return value.decode(errors="replace") if isinstance(value, bytes) else value
    # numpy/pandas scalars expose .item() -> the equivalent native Python scalar
    if hasattr(value, "item") and not isinstance(value, (dict, list, tuple)):
        try:
            value = value.item()
        except (ValueError, AttributeError, TypeError):
            pass
    if isinstance(value, float):
        return None if (math.isnan(value) or math.isinf(value)) else value
    if isinstance(value, int):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    return str(value)


def _encode_result(name: str, result) -> str:
    """Serialise one tool result for the model, sanitised and size-capped."""
    encoded = json.dumps(_json_safe(result), allow_nan=False)
    if len(encoded) > MAX_TOOL_RESULT_CHARS:
        # Tell the model it's truncated rather than letting it silently reason on a fragment.
        keep = encoded[:MAX_TOOL_RESULT_CHARS]
        return json.dumps(
            {
                "truncated": True,
                "note": f"{name} returned {len(encoded)} chars, truncated to "
                f"{MAX_TOOL_RESULT_CHARS}. Narrow the scope or filters and call it again.",
                "partial": keep,
            }
        )
    return encoded


def _dispatch(name: str, args: dict):
    tool = STUBS.get(name)
    if tool is None:
        return {"error": f"unknown tool {name!r}"}
    try:
        return tool(**args)
    except TypeError as e:  # model sent the wrong arg names — tell it, don't crash the run
        return {"error": f"bad arguments for {name}: {e}"}
    except Exception as e:  # noqa: BLE001
        # The Phase 2 contract says tools never raise and return a structured error instead.
        # This is the backstop for when they do anyway: a KeyError on a missing column would
        # otherwise take down the whole request instead of letting the agent recover or explain.
        return {"error": f"{name} failed: {type(e).__name__}: {e}"}


def _parse_final(text: str) -> dict:
    """The model's final JSON. Tolerate code fences and non-JSON prose."""
    body = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        parsed = json.loads(body)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def tool_calling_loop(client, query: str) -> AgentResult:
    """Full agent run: intent parse -> tool selection -> dispatch -> validated AgentResult."""
    messages = [{"role": "user", "content": query}]
    invoked: list[str] = []  # the transparency feed: what actually got dispatched, in order
    reply = None

    for _ in range(MAX_ITERATIONS):
        reply = client.chat_with_tools(messages, TOOL_SCHEMAS, SYSTEM)
        if not reply.tool_calls:
            break

        results = []
        for call in reply.tool_calls:
            result = _dispatch(call["name"], call["input"])
            invoked.append(call["name"])
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": call["id"],
                    "content": _encode_result(call["name"], result),
                }
            )
        messages.append({"role": "assistant", "content": reply.assistant_content})
        messages.append({"role": "user", "content": results})

    final = _parse_final(reply.text if reply else "")
    reasons = {s.get("name"): s.get("reason", "") for s in final.get("skipped", []) if isinstance(s, dict)}
    skipped = [
        SkippedTool(name=name, reason=reasons.get(name) or "not needed for this query")
        for name in STUBS
        if name not in invoked
    ]

    flagged = []
    for item in final.get("flagged_items", []):
        try:
            flagged.append(FlaggedItem.model_validate(item))
        except ValidationError:
            continue  # the model made up a malformed item; drop it rather than fail the run

    return AgentResult(
        query=query,
        summary=final.get("summary") or (reply.text if reply else ""),
        execution_summary=ExecutionSummary(
            intent_detected=final.get("intent") or "unknown",
            filters_applied=final.get("filters") if isinstance(final.get("filters"), dict) else {},
            tools_invoked=list(dict.fromkeys(invoked)),
            tools_skipped=skipped,
        ),
        flagged_items=flagged,
    )
