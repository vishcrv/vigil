import { useState } from 'react'
import { AlertTriangle, Info, ShieldCheck } from 'lucide-react'
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

export default function FlaggedItemsTable({ items }: { items: FlaggedItem[] }) {
  return (
    <section className="glass overflow-hidden rounded-xl">
      <header className="flex items-center gap-2 border-b border-border/60 px-5 py-3.5">
        <h2 className="text-[11px] font-medium tracking-wide text-muted-foreground uppercase">
          Flagged items
        </h2>
        <span className="rounded-full bg-muted px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
          {items.length}
        </span>
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
            {items.map((item) => (
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
