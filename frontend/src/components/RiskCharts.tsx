import Plot from 'react-plotly.js'
import type { FlaggedItem } from '../types'
import { useTheme } from '../theme'

// Plotly's color parser doesn't understand oklch(), so these are hex
// equivalents of the oklch tokens defined in index.css - keep in sync.
const PALETTE = {
  light: {
    high: '#DC2626',
    medium: '#C2760A',
    low: '#16803D',
    primary: '#2154C7',
    grid: '#E4E7EC',
    text: '#64748B',
  },
  dark: {
    high: '#F0938B',
    medium: '#E8C170',
    low: '#8FDBAA',
    primary: '#9DBDF5',
    grid: '#2E3136',
    text: '#9AA0AA',
  },
} as const

const fallback = '#94A3B8'

function countBy<T>(items: T[], key: (item: T) => string) {
  const counts = new Map<string, number>()
  for (const item of items) {
    const k = key(item)
    counts.set(k, (counts.get(k) ?? 0) + 1)
  }
  return counts
}

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

export default function RiskCharts({ items }: { items: FlaggedItem[] }) {
  const { resolved } = useTheme()
  const colors = PALETTE[resolved]

  const byRisk = countBy(items, (i) => i.risk_level)
  const byDay = new Map([...countBy(items, (i) => i.timestamp.slice(0, 10))].sort())
  const riskColorMap: Record<string, string> = {
    HIGH: colors.high,
    MEDIUM: colors.medium,
    LOW: colors.low,
  }

  const layout = {
    margin: { l: 40, r: 12, t: 8, b: 36 },
    height: 240,
    paper_bgcolor: 'transparent',
    plot_bgcolor: 'transparent',
    font: { family: 'inherit', size: 11, color: colors.text },
    showlegend: false,
    xaxis: { gridcolor: colors.grid, zeroline: false, linecolor: colors.grid },
    yaxis: { title: { text: 'Flags' }, dtick: 1, gridcolor: colors.grid, zeroline: false },
  } as const

  const config = { displayModeBar: false, responsive: true } as const

  return (
    <div className="grid gap-4 sm:grid-cols-2">
      <ChartCard title="Risk distribution">
        <Plot
          config={config}
          data={[
            {
              type: 'bar',
              x: [...byRisk.keys()],
              y: [...byRisk.values()],
              marker: { color: [...byRisk.keys()].map((r) => riskColorMap[r] ?? fallback) },
              hovertemplate: '%{x}: %{y} flagged<extra></extra>',
            },
          ]}
          layout={layout}
          style={{ width: '100%' }}
        />
      </ChartCard>

      <ChartCard title="Flagged transactions over time">
        <Plot
          config={config}
          data={[
            {
              type: 'scatter',
              mode: 'lines+markers',
              x: [...byDay.keys()],
              y: [...byDay.values()],
              line: { color: colors.primary },
              marker: { size: 7, color: colors.primary },
              hovertemplate: '%{x}: %{y} flagged<extra></extra>',
            },
          ]}
          layout={layout}
          style={{ width: '100%' }}
        />
      </ChartCard>
    </div>
  )
}
