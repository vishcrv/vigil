"""SQLite persistence for the audit trail: one `queries` row per /analyze run, one `flags` row
per flagged item. stdlib sqlite3, single file, no migrations (spec.md: Persisted history).
"""

import json
import os
import sqlite3
from datetime import datetime, timezone

from dotenv import load_dotenv

from schemas import AgentResult, FlaggedItem

load_dotenv()

SCHEMA = """
CREATE TABLE IF NOT EXISTS queries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    query_text      TEXT NOT NULL,
    timestamp       TEXT NOT NULL,
    intent_detected TEXT,
    filters_applied TEXT,
    tools_invoked   TEXT,
    tools_skipped   TEXT
);

CREATE TABLE IF NOT EXISTS flags (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    query_id          INTEGER NOT NULL REFERENCES queries(id),
    customer_id       TEXT NOT NULL,
    transaction_id    TEXT NOT NULL,
    -- amount/timestamp are not in spec.md's flags table, but without them a persisted flag
    -- can't be rendered back into the UI table (which shows amount and plots timestamp).
    amount            REAL,
    timestamp         TEXT,
    risk_level        TEXT,
    pattern_detected  TEXT,
    anomaly_score     REAL,
    explanation       TEXT,
    escalation_action TEXT,          -- agent's *recommended* action, set at insert time
    escalated_at      TEXT           -- NULL until a human escalates via escalate_flag()
);

CREATE INDEX IF NOT EXISTS idx_flags_query_id       ON flags(query_id);
CREATE INDEX IF NOT EXISTS idx_flags_transaction_id ON flags(transaction_id);
CREATE INDEX IF NOT EXISTS idx_flags_customer_id    ON flags(customer_id);
"""


def get_connection(path: str | None = None) -> sqlite3.Connection:
    """Open a connection. Defaults to SQLITE_PATH env var, else ./aml_agent.db."""
    conn = sqlite3.connect(path or os.getenv("SQLITE_PATH") or "./aml_agent.db")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create tables + indexes if absent. Idempotent, safe on every startup."""
    conn.executescript(SCHEMA)
    conn.commit()


def insert_query(conn: sqlite3.Connection, result: AgentResult) -> int:
    """Persist one /analyze run's execution summary. Returns the new queries.id."""
    s = result.execution_summary
    cur = conn.execute(
        "INSERT INTO queries (query_text, timestamp, intent_detected, filters_applied,"
        " tools_invoked, tools_skipped) VALUES (?, ?, ?, ?, ?, ?)",
        (
            result.query,
            datetime.now(timezone.utc).isoformat(),
            s.intent_detected,
            json.dumps(s.filters_applied),
            json.dumps(s.tools_invoked),
            json.dumps([t.model_dump() for t in s.tools_skipped]),
        ),
    )
    conn.commit()
    return cur.lastrowid


def insert_flags(conn: sqlite3.Connection, query_id: int, items: list[FlaggedItem]) -> list[int]:
    """Persist one flags row per flagged item. Returns new flag ids, in `items` order."""
    ids = []
    for f in items:
        cur = conn.execute(
            "INSERT INTO flags (query_id, customer_id, transaction_id, amount, timestamp,"
            " risk_level, pattern_detected, anomaly_score, explanation, escalation_action)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                query_id,
                f.customer_id,
                f.transaction_id,
                f.amount,
                f.timestamp.isoformat(),
                f.risk_level,
                f.pattern_detected,
                f.anomaly_score,
                f.explanation,
                f.escalation_action,
            ),
        )
        ids.append(cur.lastrowid)
    conn.commit()
    return ids


def escalate_flag(conn: sqlite3.Connection, flag_id: int, action: str) -> bool:
    """Mark a flag as actually escalated by a human. False if flag_id doesn't exist."""
    cur = conn.execute(
        "UPDATE flags SET escalated_at = ?, escalation_action = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(), action, flag_id),
    )
    conn.commit()
    return cur.rowcount > 0


def list_escalated_flags(conn: sqlite3.Connection) -> list[dict]:
    """Every flag a human has actually escalated, newest decision first.

    Joined back to `queries` so the audit view can show what was being investigated when the
    flag was raised — a row reading "customer X, CRITICAL, REPORT" is far less useful without
    the question that surfaced it. Only rows with `escalated_at` set are returned: an agent
    recommendation nobody acted on is not an escalation.
    """
    rows = conn.execute(
        """
        SELECT f.id AS flag_id, f.customer_id, f.transaction_id, f.amount, f.timestamp,
               f.risk_level, f.pattern_detected, f.anomaly_score, f.explanation,
               f.escalation_action, f.escalated_at,
               q.query_text, q.timestamp AS query_timestamp
        FROM flags f
        LEFT JOIN queries q ON q.id = f.query_id
        WHERE f.escalated_at IS NOT NULL
        ORDER BY f.escalated_at DESC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def dashboard_stats(conn: sqlite3.Connection) -> dict:
    """Aggregates over the whole audit trail, for the dashboard.

    Everything here is already recorded per `/analyze` run; none of it was surfaced anywhere.
    Counting in SQL rather than in the client because the client only ever holds the current
    session, and the point of the dashboard is what has happened across all of them.
    """
    def rows(sql: str) -> list[dict]:
        return [dict(r) for r in conn.execute(sql).fetchall()]

    totals = dict(
        conn.execute(
            """
            SELECT (SELECT count(*) FROM queries)                              AS queries,
                   (SELECT count(*) FROM flags)                                AS flags,
                   (SELECT count(*) FROM flags WHERE escalated_at IS NOT NULL) AS escalated
            """
        ).fetchone()
    )

    # Tool usage lives as a JSON array per query row, so it is counted here rather than in SQL.
    tool_counts: dict[str, int] = {}
    for row in conn.execute("SELECT tools_invoked FROM queries").fetchall():
        try:
            for tool in json.loads(row["tools_invoked"] or "[]"):
                tool_counts[str(tool)] = tool_counts.get(str(tool), 0) + 1
        except (json.JSONDecodeError, TypeError):
            continue

    return {
        "totals": totals,
        "by_risk": rows(
            "SELECT risk_level AS label, count(*) AS value FROM flags "
            "GROUP BY risk_level ORDER BY value DESC"
        ),
        "by_pattern": rows(
            "SELECT pattern_detected AS label, count(*) AS value FROM flags "
            "WHERE pattern_detected IS NOT NULL AND pattern_detected != 'NONE' "
            "GROUP BY pattern_detected ORDER BY value DESC LIMIT 10"
        ),
        "top_accounts": rows(
            "SELECT customer_id AS label, count(*) AS value FROM flags "
            "GROUP BY customer_id ORDER BY value DESC LIMIT 8"
        ),
        "queries_by_day": rows(
            "SELECT substr(timestamp, 1, 10) AS label, count(*) AS value FROM queries "
            "GROUP BY label ORDER BY label"
        ),
        "by_tool": [
            {"label": name, "value": count}
            for name, count in sorted(tool_counts.items(), key=lambda kv: -kv[1])
        ],
        "recent_queries": rows(
            "SELECT query_text AS label, intent_detected AS intent, timestamp "
            "FROM queries ORDER BY id DESC LIMIT 8"
        ),
    }
