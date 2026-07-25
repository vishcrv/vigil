import type { ExecutionSummary } from '../types'

export default function ExecutionSummaryPanel({
  summary,
  narrative,
}: {
  summary: ExecutionSummary
  narrative: string
}) {
  return (
    <section className="panel decision-flow">
      <header>
        <h2>Decision flow</h2>
        <span className="intent">{summary.intent_detected}</span>
      </header>

      <p className="narrative">{narrative}</p>

      <div className="decision-grid">
        <div>
          <h3>Filters applied</h3>
          <dl className="filters">
            {Object.entries(summary.filters_applied).map(([key, value]) => (
              <div key={key}>
                <dt>{key}</dt>
                <dd>{Array.isArray(value) ? value.join(' → ') : String(value)}</dd>
              </div>
            ))}
          </dl>
        </div>

        <div>
          <h3>Tools invoked</h3>
          <ul className="tools">
            {summary.tools_invoked.map((name) => (
              <li key={name} className="invoked">
                {name}
              </li>
            ))}
          </ul>
        </div>

        <div>
          <h3>Tools skipped</h3>
          <ul className="tools skipped-list">
            {summary.tools_skipped.map((tool) => (
              <li key={tool.name} className="skipped">
                <span className="tool-name">{tool.name}</span>
                <span className="reason">{tool.reason}</span>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </section>
  )
}
