import type { FlaggedItem } from '../types'

export function toCsv(items: FlaggedItem[]): string {
  const header = [
    'customer_id', 'transaction_id', 'amount', 'timestamp', 'risk_level',
    'pattern_detected', 'anomaly_score', 'escalation_action', 'escalated_at', 'explanation',
  ]
  const escape = (value: unknown) => {
    const text = value === null || value === undefined ? '' : String(value)
    // Explanations contain commas and quotes; RFC 4180 doubling keeps Excel happy.
    return /[",\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text
  }
  return [
    header.join(','),
    ...items.map((i) =>
      [
        i.customer_id, i.transaction_id, i.amount, i.timestamp, i.risk_level,
        i.pattern_detected, i.anomaly_score, i.escalation_action, i.escalated_at ?? '',
        i.explanation,
      ]
        .map(escape)
        .join(','),
    ),
  ].join('\n')
}

export function downloadCsv(items: FlaggedItem[]) {
  const blob = new Blob([toCsv(items)], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = `vigil-flags-${new Date().toISOString().slice(0, 10)}.csv`
  link.click()
  URL.revokeObjectURL(url)
}
