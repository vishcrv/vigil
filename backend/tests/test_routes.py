"""Route-level checks: the flag_id hand-back and the /escalate 404 path.

Both routes are exercised without an LLM key — /analyze's agent loop is monkeypatched,
since what's under test here is persistence + response wiring, not the loop itself.
"""

import os

import pytest
from fastapi.testclient import TestClient

from schemas import AgentResult, ExecutionSummary, FlaggedItem


def _fake_result() -> AgentResult:
    return AgentResult(
        query="test query",
        summary="two flagged",
        execution_summary=ExecutionSummary(intent_detected="PATTERN_SEARCH", tools_invoked=["eda"]),
        flagged_items=[
            FlaggedItem(
                customer_id=f"ACC{i}",
                transaction_id=f"TXN{i}",
                amount=9000.0 + i,
                timestamp="2022-09-03T02:14:00",
                risk_level="HIGH",
                pattern_detected="FAN-OUT",
                anomaly_score=0.9,
                explanation="structuring",
                escalation_action="REPORT",
            )
            for i in (1, 2)
        ],
    )


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    import api.routes.agent as route_mod

    monkeypatch.setattr(route_mod, "get_client", lambda provider: object())
    monkeypatch.setattr(route_mod, "tool_calling_loop", lambda client, query: _fake_result())
    import main

    with TestClient(main.app) as c:
        yield c


def test_analyze_returns_persisted_flag_ids(client):
    body = client.post("/api/v1/analyze", json={"query": "test query"}).json()
    ids = [item["flag_id"] for item in body["flagged_items"]]
    assert all(i is not None for i in ids), "UI needs a flag_id per row to escalate against"
    assert len(set(ids)) == 2, f"flag_ids must be distinct, got {ids}"


def test_escalate_persists_then_404s_on_unknown_flag(client):
    body = client.post("/api/v1/analyze", json={"query": "test query"}).json()
    flag_id = body["flagged_items"][0]["flag_id"]

    ok = client.post("/api/v1/escalate", json={"flag_id": flag_id, "action": "REPORT"})
    assert ok.status_code == 200 and ok.json()["escalated"] is True

    missing = client.post("/api/v1/escalate", json={"flag_id": 99999, "action": "REPORT"})
    assert missing.status_code == 404, "a bad flag_id should 404, not 500"


def test_startup_fails_fast_without_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    import main

    with pytest.raises(Exception):  # noqa: B017 - any startup failure is the point
        with TestClient(main.app):
            pass
