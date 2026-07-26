"""Hand-rolled tool-calling loop. Phase 3b: real loop, real ML tools.

The five ML tools are the real implementations, registered from `ml.tools` — the ML workstream's
hand-off surface. `STUBS` keeps its name because `tests/test_tool_contract.py` (the Phase 3b
integration gate) monkeypatches it by that name; it is the live dispatch table now.

`TOOL_SCHEMAS` also comes from `ml.tools` rather than being declared here. The two must describe
the same tools or the model calls something that does not exist, and the ML side is where the
constraints actually live: `eda` accepts a fixed catalogue of operations over an allow-listed
column set, not free-form filters, so a schema written from the outside would advertise a query
surface the tool rejects.
"""

import json
import math

from ml.tools import TOOL_SCHEMAS, TOOLS as STUBS
from pydantic import ValidationError
from schemas import (
    AgentResult,
    Evidence,
    EvidencePoint,
    ExecutionSummary,
    FlaggedItem,
    SkippedTool,
)

# A pattern search costs one `anomaly` plus `risk`+`explain` per account reported, so three
# accounts is already 7 turns before the model has said anything. At 8 the loop ran out mid-chain
# and returned an empty envelope.
MAX_ITERATIONS = 14

# Cap on one tool result's serialised size before it goes into the context. A real `eda` over
# ~5M rows can return far more than the model can read, and an unbounded result is both a
# context-limit failure and a cost problem. Truncation is visible to the model, not silent.
MAX_TOOL_RESULT_CHARS = 20_000


def _dataset_window() -> str | None:
    """Actual first/last timestamp, read from the data rather than hardcoded.

    The model needs an anchor for relative time. Without one it cannot resolve "the last 30
    days" — the data ends in September 2022 and wall-clock now is years later, so the honest
    reading of that phrase is an empty window, and the agent answers that it found nothing.
    """
    try:
        from ml.data import get_connection

        low, high = get_connection().execute(
            'SELECT min("Timestamp"), max("Timestamp") FROM enriched'
        ).fetchone()
        return f"{low} to {high}" if low and high else None
    except Exception:  # noqa: BLE001 - no data yet is not a reason to fail startup
        return None


_WINDOW = _dataset_window()

_DATA_FACTS = (
    f"""
Dataset coverage: {_WINDOW}. Relative time expressions from the analyst ("the last 30 days",
"this month", "recently") are relative to the END OF THE DATA, not to today's date. Anchor them
to the latest timestamp above and pass an explicit date_range. Never report an empty result on
the grounds that a requested window lies in the past — resolve it against the data and search.
"""
    if _WINDOW
    else ""
) + """
Named motifs the rule engine detects: FAN-OUT, FAN-IN, CYCLE, GATHER-SCATTER, SCATTER-GATHER,
BIPARTITE, STACK, RANDOM. Other AML vocabulary is not unsupported — it maps onto these and onto
the transaction fields:
- structuring / smurfing -> many deliberately small transfers. Filter to small amounts over the
  window and run the detectors on that slice; FAN-OUT and FAN-IN are the closest named motifs.
- layering / pass-through / mule activity -> STACK, CYCLE, GATHER-SCATTER, SCATTER-GATHER.
- integration / placement -> examine amount and payment-format distributions.
A term not appearing as a literal label in the data is NEVER a reason to return no findings, and
never the whole answer. Scope the slice, run `anomaly` on it, and report what the detectors and
rules actually found. "There is no label called X" is a fact about the schema, not a finding.
"""

SYSTEM = """You are an AML (anti-money-laundering) detection agent operating over a transaction
dataset. You are the agent; the person you are talking to is a human compliance analyst using you
to investigate that dataset. Never describe the analyst as an AI or as the agent — if they ask who
they are, they are the analyst operating this tool.

Call the tools you need to answer the analyst's query — only those you need:
- Questions about what the data *is* ("what data do we have", "what's in the dataset", "what
  columns/fields are available", "what period does it cover", "what payment formats appear")
  are in scope and need `eda`. Answer them with real figures — row counts, the date range, the
  distribution over a column — not with a description of what you are able to do. Listing your
  own capabilities instead of querying the dataset is never the right answer here.
- Aggregate or counting questions ("how many", "which customers made N+ transactions over X")
  usually need only `eda`. A threshold or grouping question is answered by aggregation; it does
  not need ML anomaly detection. "N or more" is a threshold on the aggregate, so pass
  `min_value` — ranking the top accounts instead answers a different question.
- When the analyst asks to list, show or give examples of transactions, write the actual rows
  out in `summary` — account, amount, timestamp, payment format for each. Your `summary` is the
  ENTIRE answer the analyst sees. Tool output is not displayed to them, so phrases like "above
  is a sample", "the following transactions" or "a sample was queried successfully" refer to
  something that does not exist on their screen and read as the tool having failed. Never write
  "above" or "below". Transcribe the concrete values, then give the total that matched.
- Single-entity suspicion checks ("is customer X suspicious") need feature_eng -> anomaly -> risk
  -> explain, scoped to that entity. They do not need `eda`.
- A pattern search already narrowed by a time window or a named motif ("structuring in the last
  30 days", "fan-out this month") should apply that filter and go straight to feature_eng ->
  anomaly -> risk -> explain on the filtered slice. Do not run a full-dataset `eda` sweep first;
  the query has already told you where to look.
  `anomaly` over a slice returns `rule_hits` for many accounts. Do not stop there: take the top
  3 accounts by hit score, and for EACH call `risk` and then `explain` scoped to that one
  account (`{"account_id": "..."}`), so every account you intend to report has its own risk
  level and explanation. Three accounts is the budget — prefer finishing three properly over
  starting more and running out of turns.

""" + _DATA_FACTS + """
If the query is not a request to analyse transaction data — a greeting, small talk, a question
about you or about the analyst, or anything otherwise out of scope — call NO tools at all, answer
directly in `summary`, and return [] for flagged_items. Never invoke a tool merely to have data to
talk about, and never quote dataset statistics in an answer that did not need them.

A question about the dataset is never out of scope, however open-ended. "What data do we have" is
a request to describe the data, so query it; only a question about *you* — what you are, what you
can do — belongs in the no-tools case above.

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
Use [] for flagged_items ONLY when the tools genuinely surfaced nothing suspicious. If your
summary names accounts, cites rule hits, or describes suspicious activity, then `flagged_items`
must contain those accounts — one entry each, populated from the `risk` and `explain` results.
Describing findings in prose while returning [] leaves the analyst's flagged-items table empty
and nothing to escalate, which reads as the run having failed. Every flagged item must come from
tool output; if you have not run `risk` and `explain` for an account, run them before reporting
it rather than inventing the fields."""


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


def _extract_json_object(text: str) -> str | None:
    """First balanced top-level {...} in `text`, or None.

    Brace counting rather than a regex because the payload nests objects and the explanation
    strings routinely contain braces and escaped quotes; a non-greedy regex stops at the first
    inner `}` and a greedy one swallows trailing prose.
    """
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False
    for i, char in enumerate(text[start:], start):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _parse_final(text: str) -> dict:
    """The model's final JSON envelope. Tolerates code fences and surrounding prose.

    The prompt asks for "ONLY a JSON object", and a strict whole-string parse enforced that
    literally: one line of preamble ("Here is the result:") or a trailing sign-off and the parse
    failed, discarding intent, filters, summary and flagged_items in one go. The visible symptom
    is an execution panel reading "unknown" with every skip reason falling back to the generic
    default, while the tools clearly ran — so the run looks broken even though the analysis
    succeeded. Smaller models add that prose often enough that this has to be tolerated.
    """
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[-1] if "\n" in body else body
        body = body.removeprefix("json").removeprefix("```").strip()
    body = body.removesuffix("```").strip()

    for candidate in (body, _extract_json_object(body)):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def _remember_finding(call: dict, result, risk_results: dict, explanations: dict) -> None:
    """Index `risk` and `explain` output by the account it concerns."""
    if not isinstance(result, dict) or "error" in result:
        return
    if call["name"] == "risk" and result.get("customer_id"):
        risk_results[str(result["customer_id"])] = result
    elif call["name"] == "explain" and result.get("explanation"):
        # explain() takes the risk result but does not echo the account, so the account comes
        # from the arguments the model passed in.
        args = call.get("input") or {}
        account = (args.get("risk_result") or {}).get("customer_id")
        if account:
            explanations[str(account)] = str(result["explanation"])


def _synthesise_flags(risk_results: dict, explanations: dict) -> list[FlaggedItem]:
    """Flags rebuilt from tool output, for when the model reported findings in prose only.

    LOW is skipped deliberately: "we looked and it is fine" is a real answer, and turning it
    into a flag would fabricate a finding rather than recover one.
    """
    rebuilt = []
    for account, result in risk_results.items():
        if result.get("risk_level") in (None, "LOW"):
            continue
        try:
            rebuilt.append(
                FlaggedItem(
                    customer_id=account,
                    transaction_id=result.get("transaction_id") or f"acct_{account}",
                    amount=result.get("amount") or 0.0,
                    timestamp=result.get("timestamp") or f"{_WINDOW.split(' to ')[-1]}".replace(
                        "/", "-"
                    ).replace(" ", "T"),
                    risk_level=result["risk_level"],
                    pattern_detected=result.get("pattern_detected") or "NONE",
                    anomaly_score=result.get("anomaly_score") or 0.0,
                    explanation=explanations.get(account)
                    or "Risk assessed from detector and rule output; no explanation returned.",
                    escalation_action=result.get("escalation_action") or "REVIEW",
                )
            )
        except ValidationError:
            continue
    return rebuilt


def _build_evidence(flagged: list[FlaggedItem]) -> Evidence | None:
    """Context for the charts: what the flagged accounts actually did, and why they fired.

    Deterministic DuckDB through the same `eda` tool the agent uses — no extra model calls, so
    this costs nothing in tokens or latency beyond two indexed queries. Returns None rather
    than empty series when there is nothing to plot, so the UI can omit the charts entirely
    instead of rendering empty axes.
    """
    accounts = sorted({item.customer_id for item in flagged if item.customer_id})
    if not accounts:
        return None

    def _series(spec: dict, key: str) -> list[EvidencePoint]:
        result = _dispatch("eda", {"query_spec": spec})
        if "error" in result:
            return []
        return [
            EvidencePoint(label=str(row[key]), value=float(row["value"]))
            for row in result.get("records", [])
            if row.get(key) is not None and isinstance(row.get("value"), (int, float))
        ]

    daily = _series(
        {
            "operation": "time_series", "interval": "day", "aggregation": "count",
            "filters": [{"column": "from_account", "op": "in", "value": accounts[:100]}],
            "limit": 60,
        },
        "bucket",
    )
    mix = _series(
        {
            "operation": "group", "source": "rule_hits", "dimension": "rule",
            "aggregation": "count",
            "filters": [{"column": "account", "op": "in", "value": accounts[:100]}],
            "limit": 10,
        },
        "dimension",
    )
    if not daily and not mix:
        return None
    return Evidence(accounts=accounts, daily_activity=daily, rule_mix=mix)


def tool_calling_loop(client, query: str) -> AgentResult:
    """Full agent run: intent parse -> tool selection -> dispatch -> validated AgentResult."""
    messages = [{"role": "user", "content": query}]
    invoked: list[str] = []  # the transparency feed: what actually got dispatched, in order
    # Tool output kept as it goes past, so a flag can be built from what the tools actually
    # returned instead of from the model's retyping of it. See _synthesise_flags.
    risk_results: dict[str, dict] = {}
    explanations: dict[str, str] = {}
    reply = None

    for _ in range(MAX_ITERATIONS):
        reply = client.chat_with_tools(messages, TOOL_SCHEMAS, SYSTEM)
        if not reply.tool_calls:
            break

        results = []
        for call in reply.tool_calls:
            result = _dispatch(call["name"], call["input"])
            invoked.append(call["name"])
            _remember_finding(call, result, risk_results, explanations)
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": call["id"],
                    "content": _encode_result(call["name"], result),
                }
            )
        messages.append({"role": "assistant", "content": reply.assistant_content})
        messages.append({"role": "user", "content": results})

    # Leaving the loop with tool calls still pending means the iteration budget ran out before
    # the model wrote its answer. `reply.text` is empty in that case, so everything downstream
    # falls back: intent "unknown", no filters, no summary, no flagged items — a run that did all
    # the analysis and reported none of it. Give it one turn to finish, with tools closed off.
    if reply is not None and reply.tool_calls:
        messages.append(
            {
                "role": "user",
                "content": "Tool budget reached. Do not call any more tools. Reply now with "
                "ONLY the final JSON object, populated from the tool results you already have.",
            }
        )
        try:
            reply = client.chat_with_tools(messages, TOOL_SCHEMAS, SYSTEM)
        except Exception:  # noqa: BLE001 - a failed salvage must not lose the whole run
            pass

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

    # The model routinely describes a CRITICAL account in prose and then returns [] here, which
    # leaves the table empty and nothing to escalate — the run looks like it failed when the
    # analysis in fact succeeded. Transcribing nine fields per flag is the unreliable step, and
    # it is one the model does not need to perform: `risk` and `explain` already returned
    # exactly those fields. Rebuild from the tool output when the model omitted it.
    if not flagged:
        flagged = _synthesise_flags(risk_results, explanations)

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
        evidence=_build_evidence(flagged),
    )
