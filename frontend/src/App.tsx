import { useCallback, useEffect, useRef, useState } from 'react'
import { AlertCircle, LayoutDashboard, MessageSquare, ShieldAlert } from 'lucide-react'
import DashboardView from './components/DashboardView'
import EscalationsView from './components/EscalationsView'
import ExecutionSummaryPanel from './components/ExecutionSummaryPanel'
import FlaggedItemsTable from './components/FlaggedItemsTable'
import RiskCharts from './components/RiskCharts'
import Sidebar from './components/layout/Sidebar'
import ChatScrollArea from './components/chat/ChatScrollArea'
import { AssistantMessage, UserMessage } from './components/chat/ChatMessage'
import ChatComposer from './components/chat/ChatComposer'
import ThinkingIndicator from './components/chat/ThinkingIndicator'
import { analyzeStream } from './api'
import { cn } from './lib/utils'
import type { AgentResult } from './types'

export interface ChatEntry {
  id: string
  query: string
  result: AgentResult | null
  error: string | null
}

// One per routing path the agent supports — entity lookup, pattern search, aggregate — and each
// verified against the live dataset to return something worth looking at. The previous set led
// with account 8000EBD30, which has no motif and is not an outlier: a correct answer, but it
// renders as an empty flagged-items table, which is the worst possible first impression.
const EXAMPLES = [
  'Is customer 1004286A8 suspicious?',
  'Find structuring patterns in the last 30 days',
  'What data do we have?',
]

function Hero({
  composerSeed,
  loading,
  onSubmit,
  onPick,
}: {
  composerSeed: { value: string; key: number }
  loading: boolean
  onSubmit: (text: string) => void
  onPick: (q: string) => void
}) {
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-6 px-6">
      <h1 className="text-center text-[28px] font-medium tracking-tight text-foreground/90">
        What should we look into?
      </h1>

      <ChatComposer
        autoFocus
        className="max-w-2xl"
        initialValue={composerSeed.value}
        key={composerSeed.key}
        loading={loading}
        onSubmit={onSubmit}
      />

      <div className="flex max-w-2xl flex-wrap justify-center gap-2">
        {EXAMPLES.map((q) => (
          <button
            className="rounded-full border border-border bg-card px-3.5 py-1.5 text-xs text-foreground/80 transition-colors hover:border-primary/40 hover:bg-accent hover:text-foreground"
            key={q}
            onClick={() => onPick(q)}
            type="button"
          >
            {q}
          </button>
        ))}
      </div>
    </div>
  )
}

function AgentResponse({
  result,
  onInvestigate,
}: {
  result: AgentResult
  onInvestigate: (accountId: string) => void
}) {
  // Not every query is an analysis. A greeting or a request for clarification comes back with
  // no tools invoked and nothing flagged — rendering the decision-flow panel, a header-only
  // table and two empty charts for that is noise. Show only what the run actually produced.
  const ranTools = result.execution_summary.tools_invoked.length > 0
  const hasFlags = result.flagged_items.length > 0

  if (!ranTools) {
    return <p className="text-[15px] leading-relaxed text-foreground/90">{result.summary}</p>
  }

  return (
    <>
      <ExecutionSummaryPanel narrative={result.summary} summary={result.execution_summary} />
      {/* Aggregate queries answer in the summary without flagging individual rows; the table
          and charts have nothing to plot in that case. */}
      {hasFlags && (
        <>
          <FlaggedItemsTable items={result.flagged_items} onInvestigate={onInvestigate} />
          <RiskCharts evidence={result.evidence} items={result.flagged_items} />
        </>
      )}
    </>
  )
}

type Tab = 'investigate' | 'escalations' | 'dashboard'

// Survives a reload. A demo that loses its transcript on an accidental refresh is worse than
// one that never had history; the flags themselves are already durable in SQLite, this just
// keeps the conversation around them.
const STORAGE_KEY = 'vigil.chat.v1'

function loadEntries(): ChatEntry[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    const parsed = raw ? JSON.parse(raw) : []
    // Drop anything still in flight when the page closed: it can never resolve.
    return Array.isArray(parsed)
      ? parsed.filter((e) => e && typeof e.query === 'string' && (e.result || e.error))
      : []
  } catch {
    return []
  }
}

function TabBar({ tab, onChange }: { tab: Tab; onChange: (t: Tab) => void }) {
  const tabs: { id: Tab; label: string; icon: typeof MessageSquare }[] = [
    { id: 'investigate', label: 'Investigate', icon: MessageSquare },
    { id: 'escalations', label: 'Escalations', icon: ShieldAlert },
    { id: 'dashboard', label: 'Dashboard', icon: LayoutDashboard },
  ]
  return (
    <div className="flex shrink-0 items-center gap-1 border-b border-border/60 px-4 py-2">
      {tabs.map(({ id, label, icon: Icon }) => (
        <button
          className={cn(
            'inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium transition-colors',
            tab === id
              ? 'bg-accent text-foreground'
              : 'text-muted-foreground hover:bg-accent/50 hover:text-foreground',
          )}
          key={id}
          onClick={() => onChange(id)}
          type="button"
        >
          <Icon className="size-3.5" />
          {label}
        </button>
      ))}
    </div>
  )
}

export default function App() {
  const [entries, setEntries] = useState<ChatEntry[]>(loadEntries)
  const [loading, setLoading] = useState(false)
  const [composerSeed, setComposerSeed] = useState({ value: '', key: 0 })
  const [tab, setTab] = useState<Tab>('investigate')
  // Name of the tool currently running, for the thinking indicator.
  const [activeTool, setActiveTool] = useState<string | null>(null)
  // One anchor per answer, so the sidebar can jump to a past result.
  const answerRefs = useRef<Record<string, HTMLDivElement | null>>({})
  // A run is 5-15s of tool calls; without this the only way out of a mistyped query is to wait.
  const inFlight = useRef<AbortController | null>(null)

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(entries))
    } catch {
      // Quota or private mode — losing persistence is not worth breaking the app over.
    }
  }, [entries])

  const goToAnswer = useCallback((id: string) => {
    setTab('investigate')
    // After the tab switch has painted, otherwise the node is not mounted yet.
    requestAnimationFrame(() =>
      answerRefs.current[id]?.scrollIntoView({ behavior: 'smooth', block: 'start' }),
    )
  }, [])

  function clearChat() {
    inFlight.current?.abort()
    setEntries([])
    answerRefs.current = {}
    setComposerSeed({ value: '', key: Date.now() })
  }

  async function analyze(text: string) {
    setTab('investigate')
    const id = crypto.randomUUID()
    setComposerSeed({ value: '', key: Date.now() })
    setEntries((prev) => [...prev, { id, query: text, result: null, error: null }])
    setLoading(true)
    const controller = new AbortController()
    inFlight.current = controller
    try {
      const result = await analyzeStream(text, setActiveTool, controller.signal)
      setEntries((prev) => prev.map((e) => (e.id === id ? { ...e, result } : e)))
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') {
        // Drop the placeholder entirely rather than leaving a cancelled row in the transcript.
        setEntries((prev) => prev.filter((e) => e.id !== id))
      } else {
        const message = err instanceof Error ? err.message : 'Analysis failed.'
        setEntries((prev) => prev.map((e) => (e.id === id ? { ...e, error: message } : e)))
      }
    } finally {
      inFlight.current = null
      setActiveTool(null)
      setLoading(false)
    }
  }

  function investigateAccount(accountId: string) {
    void analyze(`Is customer ${accountId} suspicious?`)
  }

  function stopAnalysis() {
    inFlight.current?.abort()
  }

  function pickQuery(q: string) {
    setComposerSeed({ value: q, key: Date.now() })
  }

  return (
    <div className="flex h-dvh overflow-hidden text-foreground">
      <Sidebar entries={entries} onClear={clearChat} onSelectQuery={goToAnswer} />

      <div className="flex min-h-0 flex-1 flex-col">
        <TabBar onChange={setTab} tab={tab} />

        {tab === 'escalations' ? (
          <EscalationsView />
        ) : tab === 'dashboard' ? (
          <DashboardView />
        ) : entries.length === 0 ? (
          <Hero composerSeed={composerSeed} loading={loading} onPick={pickQuery} onSubmit={analyze} />
        ) : (
          <>
            <ChatScrollArea>
              {entries.map((entry) => (
                <div
                  className="flex flex-col gap-5 scroll-mt-6"
                  key={entry.id}
                  ref={(node) => {
                    answerRefs.current[entry.id] = node
                  }}
                >
                  <UserMessage>{entry.query}</UserMessage>
                  <AssistantMessage>
                    {entry.error ? (
                      <div className="glass flex items-start gap-2.5 rounded-xl p-4 text-sm">
                        <AlertCircle className="mt-0.5 size-4 shrink-0 text-destructive" />
                        <div>
                          <p className="font-medium text-foreground">Analysis failed</p>
                          <p className="mt-0.5 leading-relaxed text-muted-foreground">
                            {entry.error}
                          </p>
                        </div>
                      </div>
                    ) : entry.result ? (
                      <AgentResponse
                        onInvestigate={investigateAccount}
                        result={entry.result}
                      />
                    ) : (
                      <ThinkingIndicator tool={activeTool} />
                    )}
                  </AssistantMessage>
                </div>
              ))}
            </ChatScrollArea>

            <ChatComposer
              className="mx-auto max-w-3xl px-6 pb-6"
              initialValue={composerSeed.value}
              key={composerSeed.key}
              loading={loading}
              onStop={stopAnalysis}
              onSubmit={analyze}
            />
          </>
        )}
      </div>
    </div>
  )
}
