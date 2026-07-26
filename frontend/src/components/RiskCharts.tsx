import Plot from 'react-plotly.js'
import type { Evidence, FlaggedItem } from '../types'
import { useTheme } from '../theme'

// Plotly's color parser doesn't understand oklch(), so these are hex
// equivalents of the oklch tokens defined in index.css - keep in sync.
const PALETTE = {
  light: {
    critical: '#991B1B',
    high: '#DC2626',
    medium: '#C2760A',
    low: '#16803D',
    primary: '#2154C7',
    grid: '#E4E7EC',
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

function ChartCard({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="glass rounded-xl p-5">
      <h2 className="mb-1 text-[11px] font-medium tracking-wide text-muted-foreground uppercase">
        {title}
      </h2>
      {children}
    </section>
  )
}

export default function RiskCharts({
  items,
  evidence,
}: {
  items: FlaggedItem[]
  evidence: Evidence | null
}) {
  const { resolved } = useTheme()
  const colors = PALETTE[resolved]

  // These used to plot `items` itself: a bar per risk level, and flags per day. A run flags one
  // account most of the time, so both rendered as a single bar of height one and a single dot —
  // accurate and completely uninformative. They now plot the evidence behind the flags: what the
  // flagged accounts actually did, and which motifs fired. With no evidence there is nothing
  // worth drawing, so the caller renders nothing.
  const daily = evidence?.daily_activity ?? []
  const mix = evidence?.rule_mix ?? []
  if (daily.length === 0 && mix.length === 0) return null

  const flaggedDays = new Set(items.map((i) => i.timestamp.slice(0, 10).replaceAll('-', '/')))

  const riskColorMap: Record<string, string> = {
    CRITICAL: colors.critical,
    HIGH: colors.high,
    MEDIUM: colors.medium,
    LOW: colors.low,
  }
  const topRisk = items[0]?.risk_level ?? 'HIGH'
  const accent = riskColorMap[topRisk] ?? colors.primary

  const layout = {
    margin: { l: 52, r: 12, t: 8, b: 40 },
    height: 240,
    paper_bgcolor: 'transparent',
    plot_bgcolor: 'transparent',
    font: { family: 'inherit', size: 11, color: colors.text },
    showlegend: false,
    xaxis: { gridcolor: colors.grid, zeroline: false, linecolor: colors.grid },
    yaxis: { gridcolor: colors.grid, zeroline: false },
  } as const

  const config = { displayModeBar: false, responsive: true } as const
  const who =
    evidence && evidence.accounts.length === 1
      ? evidence.accounts[0]
      : `${evidence?.accounts.length ?? 0} flagged accounts`

  return (
    <div className="grid gap-4 sm:grid-cols-2">
      {daily.length > 0 && (
        <ChartCard title={`Daily activity — ${who}`}>
          <Plot
            config={config}
            data={[
              {
                type: 'bar',
                x: daily.map((p) => p.label),
                y: daily.map((p) => p.value),
                // The day a flagged transaction landed on is picked out against the account's
                // own baseline, which is the comparison that makes the chart worth reading.
                marker: {
                  color: daily.map((p) => (flaggedDays.has(p.label) ? accent : colors.grid)),
                },
                hovertemplate: '%{x}: %{y:,} transactions<extra></extra>',
              },
            ]}
            layout={{ ...layout, yaxis: { ...layout.yaxis, title: { text: 'Transactions' } } }}
            style={{ width: '100%' }}
          />
        </ChartCard>
      )}

      {mix.length > 0 && (
        <ChartCard title="Motifs triggered">
          <Plot
            config={config}
            data={[
              {
                type: 'bar',
                orientation: 'h',
                x: mix.map((p) => p.value),
                y: mix.map((p) => p.label),
                marker: { color: accent },
                hovertemplate: '%{y}: %{x} rule hit(s)<extra></extra>',
              },
            ]}
            layout={{
              ...layout,
              margin: { ...layout.margin, l: 110 },
              xaxis: { ...layout.xaxis, title: { text: 'Rule hits' }, dtick: 1 },
            }}
            style={{ width: '100%' }}
          />
        </ChartCard>
      )}
    </div>
  )
}
