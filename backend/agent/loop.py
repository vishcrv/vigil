"""Hand-rolled tool-calling loop. Phase 3a: real loop, stubbed ML tools.

The five ML tools are stubs returning fixed plausible dicts in the shapes the ML owner agreed
(docs/IMPLEMENTATION_PLAN.md Phase 2). Phase 3b swaps STUBS for the real `backend/tools/*`
functions; nothing else here should need to change.
"""

import json

from pydantic import ValidationError
from schemas import AgentResult, ExecutionSummary, FlaggedItem, SkippedTool

MAX_ITERATIONS = 8


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

SYSTEM = """You are an AML (anti-money-laundering) detection agent over a transaction dataset.

Call the tools you need to answer the analyst's query — only those you need. Aggregate questions
usually need only `eda`; single-entity suspicion checks usually need feature_eng -> anomaly -> risk
-> explain.

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


def _dispatch(name: str, args: dict):
    tool = STUBS.get(name)
    if tool is None:
        return {"error": f"unknown tool {name!r}"}
    try:
        return tool(**args)
    except TypeError as e:  # model sent the wrong arg names — tell it, don't crash the run
        return {"error": f"bad arguments for {name}: {e}"}


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
                    "content": json.dumps(result, default=str),
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
