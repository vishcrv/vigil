"""The two routes spec.md settles on: /analyze runs the agent, /escalate records a human decision."""

import os

from fastapi import APIRouter, HTTPException

from agent.loop import tool_calling_loop
from agent.providers import get_client
from db import escalate_flag, get_connection, insert_flags, insert_query
from schemas import AgentResult, AnalyzeRequest, EscalateRequest

router = APIRouter()


@router.post("/analyze", response_model=AgentResult)
def analyze(req: AnalyzeRequest) -> AgentResult:
    client = get_client(os.getenv("LLM_PROVIDER", "anthropic"))
    result = tool_calling_loop(client, req.query)

    # Persist the audit trail, then hand the assigned flag_ids back so the UI's
    # Escalate button has a row to reference.
    with get_connection() as conn:
        query_id = insert_query(conn, result)
        flag_ids = insert_flags(conn, query_id, result.flagged_items)
    for item, flag_id in zip(result.flagged_items, flag_ids):
        item.flag_id = flag_id

    return result


@router.post("/escalate")
def escalate(req: EscalateRequest) -> dict:
    with get_connection() as conn:
        if not escalate_flag(conn, req.flag_id, req.action):
            raise HTTPException(status_code=404, detail=f"No flag with id {req.flag_id}")
    return {"flag_id": req.flag_id, "action": req.action, "escalated": True}
