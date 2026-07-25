import { useState } from 'react'
import ExecutionSummaryPanel from './components/ExecutionSummaryPanel'
import FlaggedItemsTable from './components/FlaggedItemsTable'
import RiskCharts from './components/RiskCharts'
import mockResult from './mocks/mock_agent_result.json'
import type { AgentResult } from './types'
import './App.css'

const EXAMPLE = 'Show me suspicious structuring activity in the last week'

export default function App() {
  const [query, setQuery] = useState(EXAMPLE)
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState<AgentResult | null>(null)

  // Phase 5 replaces this with POST {VITE_API_BASE_URL}/api/v1/analyze.
  async function analyze(e: React.FormEvent) {
    e.preventDefault()
    setLoading(true)
    await new Promise((r) => setTimeout(r, 600))
    setResult({ ...(mockResult as AgentResult), query })
    setLoading(false)
  }

  return (
    <main>
      <h1>
        vigil <span>AML detection agent</span>
      </h1>

      <form onSubmit={analyze}>
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Ask about transactions, accounts, or patterns…"
          aria-label="Natural language query"
        />
        <button type="submit" disabled={loading || !query.trim()}>
          {loading ? 'Analyzing…' : 'Analyze'}
        </button>
      </form>

      {!result && !loading && (
        <p className="empty">Run a query to see the agent's decision flow and findings.</p>
      )}

      {result && (
        <>
          <ExecutionSummaryPanel summary={result.execution_summary} narrative={result.summary} />
          <FlaggedItemsTable items={result.flagged_items} />
          <RiskCharts items={result.flagged_items} />
        </>
      )}
    </main>
  )
}
