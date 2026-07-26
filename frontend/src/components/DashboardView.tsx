import { useCallback, useEffect, useState } from 'react'
import Plot from 'react-plotly.js'
import { AlertCircle, RefreshCw } from 'lucide-react'
import { getStats } from '../api'
import type { DashboardStats, StatPoint } from '../types'
import { useTheme } from '../theme'
import { Button } from './ui/button'
import { cn } from '../lib/utils'

// Hex equivalents of the oklch tokens in index.css — Plotly can't read CSS custom properties.
const PALETTE = {
  light: {
    critical: '#991B1B',
    high: '#DC2626',
    medium: '#C2760A',
    low: '#16803D',
    primary: '#3B6FD4',
    grid: '#E3E6EA',
    text: '#64748B',
  },
  dark: {
    critical: '#E4645C',
    high: '#F0938B',
    medium: '#E8C170',
    low: '#8FDBAA',
    primary: '#9DBDF5',
    grid: '#2E3136',
    text: '#9AA0AA',
  },
} as const

function Stat({ label, value, hint }: { label: string; value: number; hint?: string }) {
  return (
    <div className="glass rounded-xl px-5 py-4">
      <p className="text-[11px] font-medium tracking-wide text-muted-foreground uppercase">
        {label}
      </p>
      <p className="mt-1 font-mono text-2xl text-foreground tabular-nums">
        {value.toLocaleString()}
      </p>
      {hint && <p className="mt-0.5 text-[11px] text-muted-foreground">{hint}</p>}
    </div>
  )
}

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="glass overflow-hidden rounded-xl">
      <header className="border-b border-border/60 px-5 py-3">
        <h2 className="text-[11px] font-medium tracking-wide text-muted-foreground uppercase">
          {title}
        </h2>
      </header>
      <div className="p-3">{children}</div>
    </section>
  )
}

export default function DashboardView() {
  const { resolved } = useTheme()
  const colors = PALETTE[resolved]
  const [stats, setStats] = useState<DashboardStats | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setStats(await getStats())
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load stats.')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const layout = {
    margin: { l: 44, r: 12, t: 8, b: 40 },
    height: 220,
    paper_bgcolor: 'transparent',
    plot_bgcolor: 'transparent',
    font: { family: 'inherit', size: 11, color: colors.text },
    showlegend: false,
    xaxis: { gridcolor: colors.grid, zeroline: false, linecolor: colors.grid },
    yaxis: { gridcolor: colors.grid, zeroline: false },
  } as const
  const config = { displayModeBar: false, responsive: true } as const

  const riskColor: Record<string, string> = {
    CRITICAL: colors.critical,
    HIGH: colors.high,
    MEDIUM: colors.medium,
    LOW: colors.low,
  }

  const bar = (points: StatPoint[], color: string | string[], horizontal = false) => (
    <Plot
      config={config}
      data={[
        {
          type: 'bar',
          orientation: horizontal ? 'h' : 'v',
          x: horizontal ? points.map((p) => p.value) : points.map((p) => p.label),
          y: horizontal ? points.map((p) => p.label) : points.map((p) => p.value),
          marker: { color },
          hovertemplate: horizontal ? '%{y}: %{x}<extra></extra>' : '%{x}: %{y}<extra></extra>',
        },
      ]}
      layout={horizontal ? { ...layout, margin: { ...layout.margin, l: 110 } } : layout}
      style={{ width: '100%' }}
    />
  )

  const escalationRate =
    stats && stats.totals.flags > 0
      ? Math.round((stats.totals.escalated / stats.totals.flags) * 100)
      : 0

  return (
    <div className="mx-auto flex w-full max-w-6xl flex-1 flex-col gap-4 overflow-y-auto px-6 py-6">
      <header className="flex items-center gap-3">
        <h1 className="text-[15px] font-medium text-foreground/90">Dashboard</h1>
        <Button
          className="ml-auto"
          disabled={loading}
          onClick={() => void load()}
          size="sm"
          variant="outline"
        >
          <RefreshCw className={cn('size-3.5', loading && 'animate-spin')} />
          Refresh
        </Button>
      </header>

      <p className="text-xs leading-relaxed text-muted-foreground">
        Everything recorded across every session, read from the audit trail — not just this
        conversation.
      </p>

      {error && (
        <div className="glass flex items-start gap-2.5 rounded-xl p-4 text-sm">
          <AlertCircle className="mt-0.5 size-4 shrink-0 text-destructive" />
          <div>
            <p className="font-medium text-foreground">Couldn't load the dashboard</p>
            <p className="mt-0.5 leading-relaxed text-muted-foreground">{error}</p>
          </div>
        </div>
      )}

      {stats && (
        <>
          <div className="grid gap-3 sm:grid-cols-3">
            <Stat label="Queries run" value={stats.totals.queries} />
            <Stat label="Items flagged" value={stats.totals.flags} />
            <Stat
              hint={`${escalationRate}% of flags acted on`}
              label="Escalated by an analyst"
              value={stats.totals.escalated}
            />
          </div>

          <div className="grid gap-4 lg:grid-cols-2">
            {stats.by_risk.length > 0 && (
              <Card title="Flags by risk level">
                {bar(
                  stats.by_risk,
                  stats.by_risk.map((p) => riskColor[p.label] ?? colors.primary),
                )}
              </Card>
            )}
            {stats.by_pattern.length > 0 && (
              <Card title="Motifs detected">{bar(stats.by_pattern, colors.primary, true)}</Card>
            )}
            {stats.by_tool.length > 0 && (
              <Card title="Tool usage">{bar(stats.by_tool, colors.primary, true)}</Card>
            )}
            {stats.top_accounts.length > 0 && (
              <Card title="Most-flagged accounts">
                {bar(stats.top_accounts, colors.high, true)}
              </Card>
            )}
          </div>

          {stats.recent_queries.length > 0 && (
            <Card title="Recent queries">
              <ul className="divide-y divide-border/60">
                {stats.recent_queries.map((q, i) => (
                  <li className="flex items-start gap-3 px-2 py-2.5" key={`${q.timestamp}-${i}`}>
                    <span className="min-w-0 flex-1 truncate text-xs text-foreground/85">
                      {q.label}
                    </span>
                    {q.intent && (
                      <span className="shrink-0 rounded-full bg-muted px-2 py-0.5 font-mono text-[10px] text-muted-foreground">
                        {q.intent}
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            </Card>
          )}
        </>
      )}
    </div>
  )
}
