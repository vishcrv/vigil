"""The two routes spec.md settles on: /analyze runs the agent, /escalate records a human decision."""

import json
import queue
import threading

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from agent.loop import tool_calling_loop
from agent.providers import ProviderError, get_client
from db import (
    dashboard_stats,
    escalate_flag,
    undo_escalation,
    get_connection,
    insert_flags,
    insert_query,
    list_escalated_flags,
)
from schemas import AgentResult, AnalyzeRequest, EscalatedFlag, EscalateRequest

router = APIRouter()


@router.post("/analyze", response_model=AgentResult)
def analyze(req: AnalyzeRequest) -> AgentResult:
    client = get_client()
    try:
        result = tool_calling_loop(client, req.query)
    except ProviderError as e:
        # 429 passes through as 429 so the UI can distinguish "slow down" from "broken";
        # anything else upstream becomes a 502, since the failure isn't the caller's fault.
        raise HTTPException(status_code=e.status if e.status == 429 else 502, detail=str(e)) from e

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
        if not escalate_flag(conn, req.flag_id, req.action, req.note):
            raise HTTPException(status_code=404, detail=f"No flag with id {req.flag_id}")
    return {"flag_id": req.flag_id, "action": req.action, "escalated": True}


@router.post("/escalate/undo")
def undo_escalate(req: dict) -> dict:
    """Withdraw an escalation. Escalating is one click, so mis-clicking it writes a decision a
    human did not mean to take; the audit trail needs a way back."""
    flag_id = req.get("flag_id")
    if not isinstance(flag_id, int):
        raise HTTPException(status_code=422, detail="flag_id must be an integer")
    with get_connection() as conn:
        if not undo_escalation(conn, flag_id):
            raise HTTPException(
                status_code=404, detail=f"No escalated flag with id {flag_id}"
            )
    return {"flag_id": flag_id, "escalated": False}


@router.get("/escalations", response_model=list[EscalatedFlag])
def escalations() -> list[EscalatedFlag]:
    """The audit trail: flags a human actually escalated, newest first.

    Read straight from SQLite rather than from session state, which is the point — an
    escalation recorded yesterday has to still be there after a reload, and flags raised in
    one conversation have to be visible from another.
    """
    with get_connection() as conn:
        return [EscalatedFlag.model_validate(row) for row in list_escalated_flags(conn)]


@router.get("/stats")
def stats() -> dict:
    """Dashboard aggregates over every run recorded so far."""
    with get_connection() as conn:
        return dashboard_stats(conn)


@router.post("/analyze/stream")
def analyze_stream(req: AnalyzeRequest) -> StreamingResponse:
    """Same run as /analyze, reported as Server-Sent Events while it happens.

    A query is 5-15 seconds of sequential tool calls, and until it finished the UI could only
    show a spinner. This emits each dispatch as it occurs and the finished AgentResult last, so
    the analyst watches the plan unfold. /analyze is untouched and remains the fallback.

    The loop is synchronous, so it runs on a worker thread and hands events to the generator
    through a queue rather than being rewritten as async.
    """
    events: queue.Queue = queue.Queue()

    def run() -> None:
        try:
            client = get_client()
            result = tool_calling_loop(
                client, req.query, on_event=lambda kind, payload: events.put((kind, payload))
            )
            with get_connection() as conn:
                query_id = insert_query(conn, result)
                flag_ids = insert_flags(conn, query_id, result.flagged_items)
            for item, flag_id in zip(result.flagged_items, flag_ids):
                item.flag_id = flag_id
            events.put(("result", result.model_dump(mode="json")))
        except ProviderError as exc:
            events.put(("error", {"detail": str(exc), "status": exc.status}))
        except Exception as exc:  # noqa: BLE001 - the stream must always terminate cleanly
            events.put(("error", {"detail": str(exc), "status": 500}))
        finally:
            events.put((None, None))

    threading.Thread(target=run, daemon=True).start()

    def stream():
        while True:
            kind, payload = events.get()
            if kind is None:
                break
            yield f"event: {kind}\ndata: {json.dumps(payload)}\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        # Without this a proxy or the browser may buffer the whole response, which would defeat
        # the point of streaming it.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
