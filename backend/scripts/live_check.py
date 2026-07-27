"""One live end-to-end check: real LLM, real ML tools, real SQLite, real /escalate.

Run from backend/:   python scripts/live_check.py
                     python scripts/live_check.py "Is customer 100428930 suspicious?"

Spends Gemini quota: one run is several requests. Writes to a throwaway SQLite file so it
never touches ./aml_agent.db.

This drives the FastAPI app through TestClient rather than over HTTP, so it exercises exactly
the code path `uvicorn` serves without needing a server running. Use it to verify the stack
after a change; use the UI to demo it.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Point at a scratch DB before importing anything that reads SQLITE_PATH.
os.environ["SQLITE_PATH"] = str(Path(tempfile.mkdtemp()) / "live_check.db")

from fastapi.testclient import TestClient  # noqa: E402

from db import get_connection, init_db  # noqa: E402
from main import app  # noqa: E402

DEFAULT_QUERY = "Is customer 1004286A8 suspicious?"


def main() -> None:
    query = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_QUERY

    with get_connection() as conn:
        init_db(conn)
    client = TestClient(app)

    print(f"query   : {query}")
    response = client.post("/api/v1/analyze", json={"query": query})
    print(f"status  : {response.status_code}")
    if response.status_code != 200:
        # 429 is the routine one: the free tier is small and one analyze costs several calls.
        print(f"detail  : {response.json().get('detail')}")
        raise SystemExit(1)

    body = response.json()
    summary = body["execution_summary"]
    print(f"intent  : {summary['intent_detected']}")
    print(f"filters : {summary['filters_applied']}")
    print(f"invoked : {summary['tools_invoked']}")
    print(f"skipped : {[(s['name'], s['reason']) for s in summary['tools_skipped']]}")
    print(f"summary : {body['summary'][:300]}")
    print(f"flagged : {len(body['flagged_items'])}")

    for item in body["flagged_items"]:
        print(f"\n  customer   : {item['customer_id']}")
        print(f"  risk       : {item['risk_level']} -> {item['escalation_action']}")
        print(f"  pattern    : {item['pattern_detected']}")
        print(f"  amount     : {item['amount']}  at {item['timestamp']}")
        print(f"  flag_id    : {item['flag_id']}")
        print(f"  explanation: {item['explanation'][:220]}")

        escalated = client.post(
            "/api/v1/escalate", json={"flag_id": item["flag_id"], "action": "REPORT"}
        )
        print(f"  escalate   : {escalated.status_code} {escalated.json()}")

    # Prove the audit trail landed, which is what /escalate updates and the UI reads back.
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, customer_id, risk_level, pattern_detected, escalated_at FROM flags"
        ).fetchall()
        print(f"\nflags table: {[dict(r) for r in rows]}")

    print("\nOK")


if __name__ == "__main__":
    main()
