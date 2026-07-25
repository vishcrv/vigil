// Client for the two backend routes (backend/api/routes/agent.py).
// Shapes mirror backend/schemas.py — the frozen contract, see types.ts.

import type { AgentResult, EscalationAction } from './types'

// 127.0.0.1 rather than localhost, matching .env.example: uvicorn binds IPv4 by default, but
// browsers resolve "localhost" to IPv6 ::1 first, which is refused before reaching the server.
const BASE = import.meta.env.VITE_API_BASE_URL ?? 'http://127.0.0.1:8000'

/** FastAPI returns {detail: ...} on error; surface that rather than a bare status code. */
async function post<T>(path: string, body: unknown): Promise<T> {
  let response: Response
  try {
    response = await fetch(BASE + path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
  } catch {
    // fetch only rejects on network-level failure — backend down is the likely cause locally.
    throw new Error(`Can't reach the backend at ${BASE}. Is uvicorn running?`)
  }

  if (!response.ok) {
    const detail = await response
      .json()
      .then((d) => (typeof d?.detail === 'string' ? d.detail : JSON.stringify(d?.detail)))
      .catch(() => '')
    throw new Error(detail || `${response.status} ${response.statusText}`)
  }

  return response.json() as Promise<T>
}

export function analyze(query: string): Promise<AgentResult> {
  return post<AgentResult>('/api/v1/analyze', { query })
}

export function escalate(flagId: number, action: EscalationAction): Promise<{ escalated: boolean }> {
  return post('/api/v1/escalate', { flag_id: flagId, action })
}
