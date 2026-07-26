from datetime import datetime

from db import (
    escalate_flag,
    get_connection,
    init_db,
    insert_flags,
    insert_query,
    list_escalated_flags,
)
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


def test_dashboard_stats_aggregates_the_audit_trail(tmp_path):
    """The dashboard reads across every session, so it counts in SQL rather than in the client
    (which only ever holds the current conversation)."""
    from db import dashboard_stats

    conn = get_connection(str(tmp_path / "stats.db"))
    init_db(conn)
    result = RESULT.model_copy(deep=True)
    query_id = insert_query(conn, result)
    flag_ids = insert_flags(conn, query_id, result.flagged_items)
    escalate_flag(conn, flag_ids[0], "REPORT")

    stats = dashboard_stats(conn)

    assert stats["totals"] == {
        "queries": 1,
        "flags": len(flag_ids),
        "escalated": 1,
    }
    assert {row["label"] for row in stats["by_risk"]} == {
        item.risk_level for item in result.flagged_items
    }
    # tools_invoked is stored as a JSON array per query row, so it is counted in Python.
    assert {row["label"] for row in stats["by_tool"]} == set(
        result.execution_summary.tools_invoked
    )
    assert stats["recent_queries"][0]["label"] == result.query


def test_dashboard_stats_on_an_empty_database(tmp_path):
    from db import dashboard_stats

    conn = get_connection(str(tmp_path / "empty.db"))
    init_db(conn)
    stats = dashboard_stats(conn)

    assert stats["totals"] == {"queries": 0, "flags": 0, "escalated": 0}
    assert stats["by_risk"] == [] and stats["by_tool"] == []


def test_dashboard_stats_excludes_the_none_pattern_placeholder(tmp_path):
    """`pattern_detected` is "NONE" when no motif fired — a real value in the flags table, but
    not a motif, so charting it as one would be misleading."""
    from db import dashboard_stats

    conn = get_connection(str(tmp_path / "none.db"))
    init_db(conn)
    result = RESULT.model_copy(deep=True)
    for item in result.flagged_items:
        item.pattern_detected = "NONE"
    insert_flags(conn, insert_query(conn, result), result.flagged_items)

    assert dashboard_stats(conn)["by_pattern"] == []


def test_escalation_records_the_analysts_note(tmp_path):
    """An audit trail that records the decision but not the reasoning is half a record."""
    conn = get_connection(str(tmp_path / "note.db"))
    init_db(conn)
    result = RESULT.model_copy(deep=True)
    flag_id = insert_flags(conn, insert_query(conn, result), result.flagged_items)[0]

    assert escalate_flag(conn, flag_id, "REPORT", "three deposits under the threshold") is True

    row = list_escalated_flags(conn)[0]
    assert row["escalation_note"] == "three deposits under the threshold"
    assert row["escalation_action"] == "REPORT"


def test_undo_clears_the_human_decision_but_keeps_the_flag(tmp_path):
    """Escalating is one click, so a mis-click writes a decision nobody meant to take. The
    finding itself stays — only the human action is withdrawn."""
    from db import undo_escalation

    conn = get_connection(str(tmp_path / "undo.db"))
    init_db(conn)
    result = RESULT.model_copy(deep=True)
    flag_id = insert_flags(conn, insert_query(conn, result), result.flagged_items)[0]
    escalate_flag(conn, flag_id, "REPORT", "mis-click")

    assert undo_escalation(conn, flag_id) is True
    assert list_escalated_flags(conn) == []
    assert conn.execute("SELECT count(*) FROM flags").fetchone()[0] == len(result.flagged_items)
    # Idempotent: undoing something that was never escalated is a no-op, not an error.
    assert undo_escalation(conn, flag_id) is False


def test_purge_keeps_recent_queries_and_never_drops_escalations(tmp_path):
    """The audit file grows unbounded otherwise, but a decision a human actually took is the
    part worth keeping regardless of age."""
    from db import purge_old_queries

    conn = get_connection(str(tmp_path / "purge.db"))
    init_db(conn)

    oldest_flag = None
    for i in range(12):
        result = RESULT.model_copy(deep=True)
        result.query = f"query {i}"
        flag_ids = insert_flags(conn, insert_query(conn, result), result.flagged_items)
        if i == 0:
            oldest_flag = flag_ids[0]
            escalate_flag(conn, oldest_flag, "REPORT", "keep me")

    purge_old_queries(conn, keep=5)

    remaining = {row["id"] for row in conn.execute("SELECT id FROM flags")}
    assert oldest_flag in remaining, "an escalated flag must survive the purge"
    assert conn.execute("SELECT count(*) FROM queries").fetchone()[0] <= 6


def test_migration_adds_the_note_column_to_an_existing_database(tmp_path):
    """CREATE TABLE IF NOT EXISTS is a no-op on an existing table, so a column added later has
    to be migrated in or every running install breaks on the next write."""
    path = str(tmp_path / "migrate.db")
    conn = get_connection(path)
    conn.executescript(
        "CREATE TABLE queries (id INTEGER PRIMARY KEY AUTOINCREMENT, query_text TEXT,"
        " timestamp TEXT, intent_detected TEXT, filters_applied TEXT, tools_invoked TEXT,"
        " tools_skipped TEXT);"
        "CREATE TABLE flags (id INTEGER PRIMARY KEY AUTOINCREMENT, query_id INTEGER,"
        " customer_id TEXT, transaction_id TEXT, amount REAL, timestamp TEXT, risk_level TEXT,"
        " pattern_detected TEXT, anomaly_score REAL, explanation TEXT, escalation_action TEXT,"
        " escalated_at TEXT);"
    )
    conn.commit()

    init_db(conn)  # must migrate, not fail

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(flags)")}
    assert "escalation_note" in columns
