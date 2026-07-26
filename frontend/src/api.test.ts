import { afterEach, describe, expect, it, vi } from 'vitest'
import { analyzeStream } from './api'

/** Build a Response whose body streams the given SSE text in the given chunks. */
function sseResponse(chunks: string[]): Response {
  const encoder = new TextEncoder()
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk))
      controller.close()
    },
  })
  return new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } })
}

function frame(event: string, payload: unknown): string {
  return `event: ${event}\ndata: ${JSON.stringify(payload)}\n\n`
}

const RESULT = {
  query: 'q',
  summary: 'done',
  execution_summary: {
    intent_detected: 'entity_risk_lookup',
    filters_applied: {},
    tools_invoked: ['anomaly'],
    tools_skipped: [],
  },
  flagged_items: [],
  evidence: null,
}

afterEach(() => vi.unstubAllGlobals())

describe('analyzeStream', () => {
  it('reports each tool as it starts and returns the final result', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        sseResponse([
          frame('tool_start', { name: 'anomaly' }),
          frame('tool_end', { name: 'anomaly', ok: true }),
          frame('tool_start', { name: 'risk' }),
          frame('result', RESULT),
        ]),
      ),
    )

    const seen: string[] = []
    const result = await analyzeStream('q', (name) => seen.push(name))

    expect(seen).toEqual(['anomaly', 'risk'])
    expect(result.summary).toBe('done')
  })

  it('handles a frame split across two network chunks', async () => {
    // The realistic failure: TCP does not respect message boundaries, so a naive parser that
    // assumes one chunk is one frame drops events or throws on partial JSON.
    const whole = frame('tool_start', { name: 'eda' }) + frame('result', RESULT)
    const cut = Math.floor(whole.length / 2)
    vi.stubGlobal('fetch', vi.fn(async () => sseResponse([whole.slice(0, cut), whole.slice(cut)])))

    const seen: string[] = []
    const result = await analyzeStream('q', (name) => seen.push(name))

    expect(seen).toEqual(['eda'])
    expect(result.summary).toBe('done')
  })

  it('surfaces a streamed error rather than resolving', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => sseResponse([frame('error', { detail: 'quota exhausted', status: 429 })])),
    )

    await expect(analyzeStream('q', () => {})).rejects.toThrow('quota exhausted')
  })

  it('rejects when the stream ends without a result', async () => {
    // Silence here would leave the UI spinning forever on a run that already died.
    vi.stubGlobal('fetch', vi.fn(async () => sseResponse([frame('tool_start', { name: 'eda' })])))

    await expect(analyzeStream('q', () => {})).rejects.toThrow(/without returning a result/i)
  })

  it('rejects on a non-200 response', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response('nope', { status: 502, statusText: 'Bad Gateway' })),
    )

    await expect(analyzeStream('q', () => {})).rejects.toThrow(/502/)
  })
})
