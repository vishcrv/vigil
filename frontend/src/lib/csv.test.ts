import { describe, expect, it } from 'vitest'
import { toCsv } from './csv'
import type { FlaggedItem } from '../types'

function item(overrides: Partial<FlaggedItem> = {}): FlaggedItem {
  return {
    flag_id: 1,
    customer_id: 'ACC1',
    transaction_id: 'tx_1',
    amount: 1234.5,
    timestamp: '2022-09-10T18:21:00',
    risk_level: 'CRITICAL',
    pattern_detected: 'GATHER-SCATTER',
    anomaly_score: 0.99,
    explanation: 'motifs fired',
    escalation_action: 'REPORT',
    escalated_at: null,
    ...overrides,
  }
}

describe('toCsv', () => {
  it('writes a header and one row per flag', () => {
    const lines = toCsv([item(), item({ customer_id: 'ACC2' })]).split('\n')

    expect(lines).toHaveLength(3)
    expect(lines[0].startsWith('customer_id,transaction_id,amount')).toBe(true)
    expect(lines[1]).toContain('ACC1')
    expect(lines[2]).toContain('ACC2')
  })

  it('quotes fields containing commas so columns do not shift', () => {
    // Explanations routinely contain commas. Unquoted, every column after the explanation
    // lands one place to the right and the export is silently wrong.
    const csv = toCsv([item({ explanation: 'sent to 327 receivers, then redistributed' })])

    expect(csv).toContain('"sent to 327 receivers, then redistributed"')
    expect(csv.split('\n')).toHaveLength(2)
  })

  it('doubles embedded quotes per RFC 4180', () => {
    const csv = toCsv([item({ explanation: 'flagged as "structuring"' })])

    expect(csv).toContain('"flagged as ""structuring"""')
  })

  it('quotes fields containing newlines rather than breaking the row', () => {
    const csv = toCsv([item({ explanation: 'line one\nline two' })])

    // Two data lines physically, but still one logical record, so the field must be quoted.
    expect(csv).toContain('"line one\nline two"')
  })

  it('renders a missing escalated_at as empty rather than the string null', () => {
    const csv = toCsv([item({ escalated_at: null })])

    expect(csv).not.toContain('null')
  })
})
