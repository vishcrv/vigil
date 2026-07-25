import Plot from 'react-plotly.js'
import type { FlaggedItem } from '../types'

// risk_level is a plain string (open decision), so this is a lookup with a fallback,
// not an exhaustive map.
const RISK_COLORS: Record<string, string> = {
  HIGH: '#b42318',
  MEDIUM: '#b54708',
  LOW: '#087443',
}
const fallback = '#475467'

const layout = {
  margin: { l: 48, r: 16, t: 8, b: 40 },
  height: 260,
  paper_bgcolor: 'transparent',
  plot_bgcolor: 'transparent',
  font: { family: 'inherit', size: 12, color: '#475467' },
  showlegend: false,
} as const

const config = { displayModeBar: false, responsive: true } as const

function countBy<T>(items: T[], key: (item: T) => string) {
  const counts = new Map<string, number>()
  for (const item of items) {
    const k = key(item)
    counts.set(k, (counts.get(k) ?? 0) + 1)
  }
  return counts
}

export default function RiskCharts({ items }: { items: FlaggedItem[] }) {
  const byRisk = countBy(items, (i) => i.risk_level)
  const byDay = new Map([...countBy(items, (i) => i.timestamp.slice(0, 10))].sort())

  return (
    <div className="charts">
      <section className="panel">
        <header>
          <h2>Risk distribution</h2>
        </header>
        <Plot
          data={[
            {
              type: 'bar',
              x: [...byRisk.keys()],
              y: [...byRisk.values()],
              marker: { color: [...byRisk.keys()].map((r) => RISK_COLORS[r] ?? fallback) },
              hovertemplate: '%{x}: %{y} flagged<extra></extra>',
            },
          ]}
          layout={{ ...layout, yaxis: { title: { text: 'Flags' }, dtick: 1 } }}
          config={config}
          style={{ width: '100%' }}
        />
      </section>

      <section className="panel">
        <header>
          <h2>Flagged transactions over time</h2>
        </header>
        <Plot
          data={[
            {
              type: 'scatter',
              mode: 'lines+markers',
              x: [...byDay.keys()],
              y: [...byDay.values()],
              line: { color: '#175cd3' },
              marker: { size: 8, color: '#175cd3' },
              hovertemplate: '%{x}: %{y} flagged<extra></extra>',
            },
          ]}
          layout={{ ...layout, yaxis: { title: { text: 'Flags' }, dtick: 1 } }}
          config={config}
          style={{ width: '100%' }}
        />
      </section>
    </div>
  )
}
