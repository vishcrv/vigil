from datetime import datetime

from db import escalate_flag, get_connection, init_db, insert_flags, insert_query
from schemas import AgentResult, ExecutionSummary, FlaggedItem, SkippedTool

RESULT = AgentResult(
    query="show me structuring in Q3",
    summary="Found 2 suspicious transactions.",
    execution_summary=ExecutionSummary(
        intent_detected="pattern_detection",
        filters_applied={"quarter": "Q3", "min_amount": 9000},
        tools_invoked=["detect_structuring", "get_customer_profile"],
        tools_skipped=[SkippedTool(name="network_analysis", reason="no counterparty in query")],
    ),
    flagged_items=[
        FlaggedItem(
            customer_id="C001",
            transaction_id="T001",
            amount=9500.50,
            timestamp=datetime(2024, 7, 1, 12, 30),
            risk_level="HIGH",
            pattern_detected="structuring",
            anomaly_score=0.91,
            explanation="three deposits just under 10k",
            escalation_action="REPORT",
        ),
        FlaggedItem(
            customer_id="C002",
            transaction_id="T002",
            amount=1200.0,
            timestamp=datetime(2024, 7, 2, 9, 0),
            risk_level="LOW",
            pattern_detected="round_amount",
            anomaly_score=0.35,
            explanation="round amount, low deviation",
            escalation_action="MONITOR",
        ),
    ],
)


def _db(tmp_path):
    conn = get_connection(str(tmp_path / "test.db"))
    init_db(conn)
    init_db(conn)  # idempotent
    return conn


def test_round_trip(tmp_path):
    conn = _db(tmp_path)
    qid = insert_query(conn, RESULT)
    ids = insert_flags(conn, qid, RESULT.flagged_items)

    q = conn.execute("SELECT * FROM queries WHERE id = ?", (qid,)).fetchone()
    assert q["query_text"] == RESULT.query
    assert q["intent_detected"] == "pattern_detection"
    assert q["filters_applied"] == '{"quarter": "Q3", "min_amount": 9000}'
    assert q["tools_invoked"] == '["detect_structuring", "get_customer_profile"]'
    assert (
        q["tools_skipped"]
        == '[{"name": "network_analysis", "reason": "no counterparty in query"}]'
    )

    rows = conn.execute("SELECT * FROM flags WHERE query_id = ? ORDER BY id", (qid,)).fetchall()
    assert [r["id"] for r in rows] == ids
    for row, item in zip(rows, RESULT.flagged_items):
        assert row["customer_id"] == item.customer_id
        assert row["transaction_id"] == item.transaction_id
        assert row["amount"] == item.amount
        assert row["timestamp"] == item.timestamp.isoformat()
        assert row["risk_level"] == item.risk_level
        assert row["pattern_detected"] == item.pattern_detected
        assert row["anomaly_score"] == item.anomaly_score
        assert row["explanation"] == item.explanation
        # recommended action stored, but nobody has escalated yet
        assert row["escalation_action"] == item.escalation_action
        assert row["escalated_at"] is None


def test_escalate_flag(tmp_path):
    conn = _db(tmp_path)
    qid = insert_query(conn, RESULT)
    first, second = insert_flags(conn, qid, RESULT.flagged_items)

    assert escalate_flag(conn, first, "REVIEW") is True
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM flags").fetchall()}
    assert rows[first]["escalated_at"] is not None
    assert rows[first]["escalation_action"] == "REVIEW"
    assert rows[second]["escalated_at"] is None  # untouched
    assert rows[second]["escalation_action"] == "MONITOR"

    assert escalate_flag(conn, 9999, "REPORT") is False
