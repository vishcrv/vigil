import type { FlaggedItem } from '../types'

const money = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  maximumFractionDigits: 0,
})

export default function FlaggedItemsTable({ items }: { items: FlaggedItem[] }) {
  return (
    <section className="panel">
      <header>
        <h2>Flagged items</h2>
        <span className="count">{items.length}</span>
      </header>

      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>Customer</th>
              <th>Transaction</th>
              <th className="num">Amount</th>
              <th>Risk</th>
              <th>Pattern</th>
              <th>Explanation</th>
              <th>Action</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {items.map((item) => (
              <tr key={item.transaction_id}>
                <td className="mono">{item.customer_id}</td>
                <td className="mono">{item.transaction_id}</td>
                <td className="num mono">{money.format(item.amount)}</td>
                <td>
                  <span className={`risk risk-${item.risk_level.toLowerCase()}`}>
                    {item.risk_level}
                  </span>
                </td>
                <td className="mono">{item.pattern_detected}</td>
                <td className="explanation">{item.explanation}</td>
                <td>{item.escalation_action}</td>
                <td>
                  {item.escalated_at ? (
                    <span className="escalated">
                      Escalated {new Date(item.escalated_at).toLocaleDateString()}
                    </span>
                  ) : (
                    // Phase 5 wires this to POST /api/v1/escalate.
                    <button onClick={() => console.log('escalate', item.flag_id)}>
                      Escalate
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}
