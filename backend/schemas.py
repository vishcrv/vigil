"""Frozen API contract between the agent loop, FastAPI routes, and the React frontend.

Phase 0 deliverable. Both sides build against this shape; don't change a field without
telling the other owner (see docs/IMPLEMENTATION_PLAN.md Phase 0 step 4).
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

EscalationAction = Literal["MONITOR", "REVIEW", "REPORT"]


class SkippedTool(BaseModel):
    """A tool the agent chose not to call, and why. Feeds the transparency panel."""

    name: str
    reason: str


class ExecutionSummary(BaseModel):
    """Mirrors the `queries` SQLite row. The "transparent decision flow" the UI leads with."""

    intent_detected: str
    filters_applied: dict = {}
    tools_invoked: list[str] = []
    tools_skipped: list[SkippedTool] = []


class FlaggedItem(BaseModel):
    """Mirrors a `flags` SQLite row (which persists every field here, `amount`/`timestamp` included)."""

    flag_id: int | None = None  # assigned on SQLite insert, absent before persistence
    customer_id: str
    transaction_id: str
    amount: float
    timestamp: datetime  # of the transaction itself; the UI's temporal chart plots this
    # ponytail: plain str until the ML owner settles the risk_level scale (open decision #1),
    # then tighten to Literal["LOW", "MEDIUM", "HIGH", ...] to match.
    risk_level: str
    pattern_detected: str
    anomaly_score: float
    explanation: str
    escalation_action: EscalationAction
    escalated_at: datetime | None = None


class EscalatedFlag(FlaggedItem):
    """A flag a human escalated, plus the query that surfaced it.

    Extends FlaggedItem rather than redefining it so the audit view and the in-session table
    render from the same shape; `escalated_at` is never None here by construction.
    """

    query_text: str | None = None
    query_timestamp: datetime | None = None


class AgentResult(BaseModel):
    """The full `POST /api/v1/analyze` response."""

    query: str
    summary: str  # the agent's NL answer, carries aggregate queries that flag nothing
    execution_summary: ExecutionSummary
    flagged_items: list[FlaggedItem] = []


class AnalyzeRequest(BaseModel):
    query: str


class EscalateRequest(BaseModel):
    flag_id: int
    action: EscalationAction


def demo() -> None:
    """Self-check: the mock fixture the frontend builds against must validate."""
    import json
    import pathlib

    fixture = pathlib.Path(__file__).parent.parent / "frontend/src/mocks/mock_agent_result.json"
    result = AgentResult.model_validate(json.loads(fixture.read_text()))
    assert result.flagged_items, "fixture should carry flagged items for the UI table"
    assert result.execution_summary.tools_skipped, "fixture should show a skipped tool + reason"
    assert all(f.flag_id is not None for f in result.flagged_items), "persisted items need flag_ids"
    print(f"OK: {fixture.name} validates, {len(result.flagged_items)} flagged items")


if __name__ == "__main__":
    demo()
