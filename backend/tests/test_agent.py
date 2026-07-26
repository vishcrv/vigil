"""Phase 3a checks: the loop end-to-end on a fake client, the startup key check, the iteration cap.

Run from backend/: .venv\\Scripts\\python.exe -m pytest tests/test_agent.py -q
"""

import json

import pytest
from agent import providers
from agent.loop import MAX_ITERATIONS, tool_calling_loop
from agent.providers import Reply, check_api_key
from schemas import AgentResult

FINAL_JSON = json.dumps(
    {
        "intent": "entity_risk_lookup",
        "filters": {"customer_id": "8000EBD30"},
        "summary": "Customer 8000EBD30 shows a HIGH-risk fan-out pattern.",
        "skipped": [{"name": "eda", "reason": "single-entity query, no aggregates needed"}],
        "flagged_items": [
            {
                "customer_id": "8000EBD30",
                "transaction_id": "TXN-4471902",
                "amount": 184500.0,
                "timestamp": "2022-09-01T14:22:00",
                "risk_level": "HIGH",
                "pattern_detected": "FAN-OUT",
                "anomaly_score": 0.87,
                "explanation": "Out-degree above baseline plus a currency mismatch.",
                "escalation_action": "REPORT",
            }
        ],
    }
)


def _call(tool_id, name, args):
    return {"id": tool_id, "name": name, "input": args}


class ScriptedClient:
    """Returns a fixed sequence of Replies, one per chat_with_tools call."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    def chat_with_tools(self, messages, tools, system):
        self.calls += 1
        return self.replies.pop(0)


class ForeverClient:
    """Always asks for another tool — the loop must stop it."""

    def __init__(self):
        self.calls = 0

    def chat_with_tools(self, messages, tools, system):
        self.calls += 1
        return Reply(text="", tool_calls=[_call(f"t{self.calls}", "anomaly", {"scope": {}})], assistant_content=[])


def test_loop_returns_valid_agent_result_with_transparency():
    client = ScriptedClient(
        [
            Reply("", [_call("t1", "feature_eng", {"scope": {"account_id": "8000EBD30"}})], []),
            Reply("", [_call("t2", "anomaly", {"scope": {"account_id": "8000EBD30"}})], []),
            Reply("", [_call("t3", "risk", {"anomaly_result": {"anomaly_score": 0.87}})], []),
            Reply(FINAL_JSON, [], []),
        ]
    )

    result = tool_calling_loop(client, "Is customer 8000EBD30 suspicious?")

    AgentResult.model_validate(result.model_dump())  # Pydantic-valid on the frozen contract
    assert result.execution_summary.tools_invoked == ["feature_eng", "anomaly", "risk"]
    assert result.execution_summary.intent_detected == "entity_risk_lookup"
    assert result.execution_summary.filters_applied == {"customer_id": "8000EBD30"}
    assert len(result.flagged_items) == 1
    assert result.flagged_items[0].escalation_action == "REPORT"

    skipped = {s.name: s.reason for s in result.execution_summary.tools_skipped}
    assert set(skipped) == {"eda", "explain"}  # registered but never dispatched
    assert all(reason for reason in skipped.values())  # every skip carries a reason


def test_check_api_key_raises_when_key_missing(monkeypatch):
    monkeypatch.setitem(providers.KEY_VARS, "anthropic", "TEST_FAKE_KEY_VAR")
    monkeypatch.delenv("TEST_FAKE_KEY_VAR", raising=False)
    with pytest.raises(RuntimeError, match="TEST_FAKE_KEY_VAR"):
        check_api_key("anthropic")

    monkeypatch.setenv("TEST_FAKE_KEY_VAR", "   ")  # empty-ish counts as missing
    with pytest.raises(RuntimeError, match="TEST_FAKE_KEY_VAR"):
        check_api_key("anthropic")

    with pytest.raises(RuntimeError, match="LLM_PROVIDER"):
        check_api_key("openai")


def test_loop_caps_runaway_tool_requests():
    """Bounded, and one turn past the tool budget rather than equal to it.

    A model that never stops calling tools used to exhaust the loop with nothing written, so
    the run reported no intent and no summary despite having done the work. The loop now spends
    one final turn asking it to answer from the results it already has — that is the +1. What
    the test guards is that the total stays bounded.
    """
    client = ForeverClient()
    result = tool_calling_loop(client, "loop forever please")

    assert client.calls == MAX_ITERATIONS + 1
    AgentResult.model_validate(result.model_dump())
    assert result.execution_summary.tools_invoked == ["anomaly"]


# --- final-envelope parsing ---------------------------------------------------------------

@pytest.mark.parametrize("raw", [
    '{"intent": "x"}',
    '```json\n{"intent": "x"}\n```',
    '```\n{"intent": "x"}\n```',
    'Here is the result:\n{"intent": "x"}',
    '{"intent": "x"}\nLet me know if you need anything else.',
    'Sure!\n```json\n{"intent": "x"}\n```\nHope that helps.',
])
def test_final_envelope_survives_prose_and_fences(raw):
    """The prompt asks for "ONLY a JSON object"; smaller models add a preamble or sign-off
    anyway. A strict whole-string parse discarded intent, filters, summary and flagged_items
    together, surfacing as an execution panel reading "unknown" while the tools had clearly
    run."""
    from agent.loop import _parse_final

    assert _parse_final(raw)["intent"] == "x"


def test_final_envelope_handles_nesting_and_braces_inside_strings():
    from agent.loop import _parse_final

    parsed = _parse_final(
        'text {"intent": "x", "filters": {"a": {"b": 1}}, '
        '"summary": "used {curly} and \\"quotes\\""} trailing'
    )
    assert parsed["filters"] == {"a": {"b": 1}}
    assert parsed["summary"] == 'used {curly} and "quotes"'


@pytest.mark.parametrize("raw", ["I could not complete that.", "[1, 2, 3]", "", "{unclosed"])
def test_final_envelope_falls_back_to_empty_on_unparseable(raw):
    """An empty dict is the signal the loop uses to fall back to the raw reply text."""
    from agent.loop import _parse_final

    assert _parse_final(raw) == {}
