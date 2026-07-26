import { useState } from 'react'
import { AlertCircle, MessageSquare, ShieldAlert } from 'lucide-react'
import EscalationsView from './components/EscalationsView'
import ExecutionSummaryPanel from './components/ExecutionSummaryPanel'
import FlaggedItemsTable from './components/FlaggedItemsTable'
import RiskCharts from './components/RiskCharts'
import Sidebar from './components/layout/Sidebar'
import ChatScrollArea from './components/chat/ChatScrollArea'
import { AssistantMessage, UserMessage } from './components/chat/ChatMessage'
import ChatComposer from './components/chat/ChatComposer'
import ThinkingIndicator from './components/chat/ThinkingIndicator'
import { analyze as analyzeQuery } from './api'
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

function AgentResponse({ result }: { result: AgentResult }) {
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
          <FlaggedItemsTable items={result.flagged_items} />
          <RiskCharts items={result.flagged_items} />
        </>
      )}
    </>
  )
}

type Tab = 'investigate' | 'escalations'

function TabBar({ tab, onChange }: { tab: Tab; onChange: (t: Tab) => void }) {
  const tabs: { id: Tab; label: string; icon: typeof MessageSquare }[] = [
    { id: 'investigate', label: 'Investigate', icon: MessageSquare },
    { id: 'escalations', label: 'Escalations', icon: ShieldAlert },
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
  const [entries, setEntries] = useState<ChatEntry[]>([])
  const [loading, setLoading] = useState(false)
  const [composerSeed, setComposerSeed] = useState({ value: '', key: 0 })
  const [tab, setTab] = useState<Tab>('investigate')

  async function analyze(text: string) {
    setTab('investigate')
    const id = crypto.randomUUID()
    setComposerSeed({ value: '', key: Date.now() })
    setEntries((prev) => [...prev, { id, query: text, result: null, error: null }])
    setLoading(true)
    try {
      const result = await analyzeQuery(text)
      setEntries((prev) => prev.map((e) => (e.id === id ? { ...e, result } : e)))
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Analysis failed.'
      setEntries((prev) => prev.map((e) => (e.id === id ? { ...e, error: message } : e)))
    } finally {
      setLoading(false)
    }
  }

  function pickQuery(q: string) {
    setComposerSeed({ value: q, key: Date.now() })
  }

  return (
    <div className="flex h-dvh overflow-hidden text-foreground">
      <Sidebar entries={entries} onSelectQuery={pickQuery} />

      <div className="flex min-h-0 flex-1 flex-col">
        <TabBar onChange={setTab} tab={tab} />

        {tab === 'escalations' ? (
          <EscalationsView />
        ) : entries.length === 0 ? (
          <Hero composerSeed={composerSeed} loading={loading} onPick={pickQuery} onSubmit={analyze} />
        ) : (
          <>
            <ChatScrollArea>
              {entries.map((entry) => (
                <div className="flex flex-col gap-5" key={entry.id}>
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
                      <AgentResponse result={entry.result} />
                    ) : (
                      <ThinkingIndicator />
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
              onSubmit={analyze}
            />
          </>
        )}
      </div>
    </div>
  )
}
