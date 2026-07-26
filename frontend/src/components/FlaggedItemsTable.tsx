import { useMemo, useState } from 'react'
import { AlertTriangle, ArrowUpDown, Download, Info, ShieldCheck } from 'lucide-react'
import type { FlaggedItem } from '../types'
import { escalate as escalateFlag } from '../api'
import { cn } from '../lib/utils'
import { Button } from './ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from './ui/dialog'

const money = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  maximumFractionDigits: 0,
})

const RISK_STYLE: Record<string, { icon: typeof AlertTriangle; className: string }> = {
  // CRITICAL is the top tier of the ML risk scale (backend/docs/ml_spec.md decision 1) and the
  // only level that maps to REPORT. Without an entry here it fell through to the neutral grey
  // fallback, rendering the most severe flag as if its level were unrecognised.
  CRITICAL: { icon: AlertTriangle, className: 'bg-risk-critical-bg text-risk-critical' },
  HIGH: { icon: AlertTriangle, className: 'bg-risk-high-bg text-risk-high' },
  MEDIUM: { icon: Info, className: 'bg-risk-medium-bg text-risk-medium' },
  LOW: { icon: ShieldCheck, className: 'bg-risk-low-bg text-risk-low' },
}

function RiskBadge({ level }: { level: string }) {
  const style = RISK_STYLE[level] ?? { icon: Info, className: 'bg-muted text-muted-foreground' }
  const Icon = style.icon
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-semibold tracking-wide',
        style.className,
      )}
    >
      <Icon className="size-3" />
      {level}
    </span>
  )
}

function EscalateControl({ item }: { item: FlaggedItem }) {
  const [open, setOpen] = useState(false)
  const [escalated, setEscalated] = useState(false)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)

  if (item.escalated_at || escalated) {
    return (
      <span className="text-xs font-medium text-risk-low">
        Escalated {item.escalated_at ? new Date(item.escalated_at).toLocaleDateString() : 'now'}
      </span>
    )
  }

  // flag_id is assigned on SQLite insert; absent means this result was never persisted,
  // so there's no row for /escalate to update.
  if (item.flag_id === null) {
    return <span className="text-xs text-muted-foreground">Not persisted</span>
  }

  async function confirm() {
    if (item.flag_id === null) return
    setPending(true)
    setError(null)
    try {
      await escalateFlag(item.flag_id, item.escalation_action)
      setEscalated(true)
      setOpen(false)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Escalation failed.')
    } finally {
      setPending(false)
    }
  }

  return (
    <Dialog onOpenChange={(next) => !pending && setOpen(next)} open={open}>
      <Button onClick={() => setOpen(true)} size="sm" variant="outline">
        Escalate
      </Button>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Escalate transaction {item.transaction_id}?</DialogTitle>
          <DialogDescription>
            This will record <span className="font-mono">{item.escalation_action}</span> as the
            action taken on this flag for customer {item.customer_id}.
          </DialogDescription>
        </DialogHeader>
        {error && <p className="text-sm text-destructive">{error}</p>}
        <DialogFooter>
          <Button disabled={pending} onClick={() => setOpen(false)} variant="outline">
            Cancel
          </Button>
          <Button disabled={pending} onClick={confirm}>
            {pending ? 'Escalating…' : 'Confirm'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// Highest risk first by default: the reason four tiers exist is triage order, so the table
// should open on the rows an analyst would work first.
const RISK_ORDER = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW']

type SortKey = 'risk' | 'amount'

function toCsv(items: FlaggedItem[]): string {
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

function downloadCsv(items: FlaggedItem[]) {
  const blob = new Blob([toCsv(items)], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = `vigil-flags-${new Date().toISOString().slice(0, 10)}.csv`
  link.click()
  URL.revokeObjectURL(url)
}

export default function FlaggedItemsTable({ items }: { items: FlaggedItem[] }) {
  const [sortKey, setSortKey] = useState<SortKey>('risk')
  const [riskFilter, setRiskFilter] = useState<string | null>(null)

  const visible = useMemo(() => {
    const filtered = riskFilter ? items.filter((i) => i.risk_level === riskFilter) : items
    return [...filtered].sort((a, b) =>
      sortKey === 'amount'
        ? b.amount - a.amount
        : RISK_ORDER.indexOf(a.risk_level) - RISK_ORDER.indexOf(b.risk_level),
    )
  }, [items, riskFilter, sortKey])

  const present = RISK_ORDER.filter((level) => items.some((i) => i.risk_level === level))

  return (
    <section className="glass overflow-hidden rounded-xl">
      <header className="flex flex-wrap items-center gap-2 border-b border-border/60 px-5 py-3.5">
        <h2 className="text-[11px] font-medium tracking-wide text-muted-foreground uppercase">
          Flagged items
        </h2>
        <span className="rounded-full bg-muted px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
          {visible.length}
          {riskFilter && ` / ${items.length}`}
        </span>

        {present.length > 1 &&
          present.map((level) => (
            <button
              className={cn(
                'rounded-full border px-2 py-0.5 text-[10px] font-medium transition-colors',
                riskFilter === level
                  ? 'border-transparent bg-accent text-foreground'
                  : 'border-border text-muted-foreground hover:text-foreground',
              )}
              key={level}
              onClick={() => setRiskFilter(riskFilter === level ? null : level)}
              type="button"
            >
              {level}
            </button>
          ))}

        <div className="ml-auto flex items-center gap-1.5">
          <button
            className="rounded-md px-2 py-1 text-[11px] text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
            onClick={() => setSortKey(sortKey === 'risk' ? 'amount' : 'risk')}
            title="Toggle sort"
            type="button"
          >
            <ArrowUpDown className="mr-1 inline size-3" />
            {sortKey === 'risk' ? 'Risk' : 'Amount'}
          </button>
          <button
            className="rounded-md px-2 py-1 text-[11px] text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
            onClick={() => downloadCsv(visible)}
            title="Download these rows as CSV"
            type="button"
          >
            <Download className="mr-1 inline size-3" />
            CSV
          </button>
        </div>
      </header>

      <div className="scroll-thin overflow-x-auto">
        <table className="w-full min-w-[820px] border-collapse text-sm">
          <thead>
            <tr className="border-b border-border text-left text-[11px] tracking-wide text-muted-foreground uppercase">
              <th className="px-5 py-2 font-medium">Customer</th>
              <th className="px-3 py-2 font-medium">Transaction</th>
              <th className="px-3 py-2 text-right font-medium">Amount</th>
              <th className="px-3 py-2 font-medium">Risk</th>
              <th className="px-3 py-2 font-medium">Pattern</th>
              <th className="px-3 py-2 font-medium">Explanation</th>
              <th className="px-3 py-2 font-medium">Action</th>
              <th className="px-5 py-2 font-medium" />
            </tr>
          </thead>
          <tbody>
            {visible.map((item) => (
              <tr
                className="border-b border-border/60 align-top transition-colors last:border-0 hover:bg-accent/40"
                key={item.transaction_id}
              >
                <td className="px-5 py-3 font-mono text-xs text-foreground/90">
                  {item.customer_id}
                </td>
                <td className="px-3 py-3 font-mono text-xs text-foreground/90">
                  {item.transaction_id}
                </td>
                <td className="px-3 py-3 text-right font-mono text-xs tabular-nums text-foreground">
                  {money.format(item.amount)}
                </td>
                <td className="px-3 py-3">
                  <RiskBadge level={item.risk_level} />
                </td>
                <td className="px-3 py-3 font-mono text-xs text-muted-foreground">
                  {item.pattern_detected}
                </td>
                <td className="min-w-[280px] max-w-[420px] px-3 py-3 text-xs leading-relaxed text-foreground/80">
                  {item.explanation}
                </td>
                <td className="px-3 py-3 text-xs font-medium text-foreground/80">
                  {item.escalation_action}
                </td>
                <td className="px-5 py-3">
                  <EscalateControl item={item} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}
