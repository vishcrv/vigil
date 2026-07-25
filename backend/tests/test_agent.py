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
    client = ForeverClient()
    result = tool_calling_loop(client, "loop forever please")

    assert client.calls == MAX_ITERATIONS
    AgentResult.model_validate(result.model_dump())
    assert result.execution_summary.tools_invoked == ["anomaly"]
