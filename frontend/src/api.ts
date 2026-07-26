// Client for the two backend routes (backend/api/routes/agent.py).
// Shapes mirror backend/schemas.py — the frozen contract, see types.ts.

import type {
  AgentResult,
  DashboardStats,
  EscalatedFlag,
  EscalationAction,
} from './types'

// 127.0.0.1 rather than localhost, matching .env.example: uvicorn binds IPv4 by default, but
// browsers resolve "localhost" to IPv6 ::1 first, which is refused before reaching the server.
const BASE = import.meta.env.VITE_API_BASE_URL ?? 'http://127.0.0.1:8000'

/** FastAPI returns {detail: ...} on error; surface that rather than a bare status code. */
async function post<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  let response: Response
  try {
    response = await fetch(BASE + path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal,
    })
  } catch (err) {
    // An aborted request is a user action, not a backend problem — let it through untouched so
    // the caller can tell the two apart.
    if (err instanceof DOMException && err.name === 'AbortError') throw err
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

async function get<T>(path: string): Promise<T> {
  let response: Response
  try {
    response = await fetch(BASE + path)
  } catch {
    throw new Error(`Can't reach the backend at ${BASE}. Is uvicorn running?`)
  }
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`)
  return response.json() as Promise<T>
}

/** The audit trail, read from SQLite — survives reloads and spans conversations. */
export function getEscalations(): Promise<EscalatedFlag[]> {
  return get<EscalatedFlag[]>('/api/v1/escalations')
}

export function getStats(): Promise<DashboardStats> {
  return get<DashboardStats>('/api/v1/stats')
}

export function analyze(query: string, signal?: AbortSignal): Promise<AgentResult> {
  return post<AgentResult>('/api/v1/analyze', { query }, signal)
}

export function escalate(
  flagId: number,
  action: EscalationAction,
  note?: string,
): Promise<{ escalated: boolean }> {
  return post('/api/v1/escalate', { flag_id: flagId, action, note: note || null })
}

/** Withdraw an escalation — escalating is one click, so it has to be reversible. */
export function undoEscalation(flagId: number): Promise<{ escalated: boolean }> {
  return post('/api/v1/escalate/undo', { flag_id: flagId })
}

/**
 * Same run as `analyze`, streamed. Reports each tool dispatch as it happens so the UI can show
 * progress instead of a spinner for 5-15 seconds.
 *
 * Uses fetch rather than EventSource because EventSource cannot issue a POST. Falls back to the
 * caller's error handling on any transport failure; `analyze` remains available unchanged.
 */
export async function analyzeStream(
  query: string,
  onTool: (name: string) => void,
  signal?: AbortSignal,
): Promise<AgentResult> {
  const response = await fetch(BASE + '/api/v1/analyze/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ query }),
    signal,
  })
  if (!response.ok || !response.body) {
    throw new Error(`${response.status} ${response.statusText}`)
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let result: AgentResult | null = null
  let failure: string | null = null

  // SSE frames are separated by a blank line; a chunk can split one, so parse only whole frames.
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })

    let split = buffer.indexOf('\n\n')
    while (split !== -1) {
      const frame = buffer.slice(0, split)
      buffer = buffer.slice(split + 2)
      split = buffer.indexOf('\n\n')

      const event = /^event: (.*)$/m.exec(frame)?.[1]
      const raw = /^data: (.*)$/m.exec(frame)?.[1]
      if (!event || !raw) continue
      const payload = JSON.parse(raw)
      if (event === 'tool_start') onTool(payload.name)
      else if (event === 'result') result = payload as AgentResult
      else if (event === 'error') failure = payload.detail
    }
  }

  if (failure) throw new Error(failure)
  if (!result) throw new Error('The analysis ended without returning a result.')
  return result
}
