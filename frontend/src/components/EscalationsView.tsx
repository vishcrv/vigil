import { useCallback, useEffect, useState } from 'react'
import { AlertCircle, RefreshCw, ShieldCheck } from 'lucide-react'
import { getEscalations } from '../api'
import type { EscalatedFlag } from '../types'
import { Button } from './ui/button'
import { cn } from '../lib/utils'

const money = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  maximumFractionDigits: 0,
})

// Mirrors FlaggedItemsTable's RISK_STYLE so a CRITICAL row reads identically in both views.
const RISK_CLASS: Record<string, string> = {
  CRITICAL: 'bg-risk-critical-bg text-risk-critical',
  HIGH: 'bg-risk-high-bg text-risk-high',
  MEDIUM: 'bg-risk-medium-bg text-risk-medium',
  LOW: 'bg-risk-low-bg text-risk-low',
}

function when(iso: string | null): string {
  if (!iso) return '—'
  const date = new Date(iso)
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString()
}

export default function EscalationsView() {
  const [rows, setRows] = useState<EscalatedFlag[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setRows(await getEscalations())
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load escalations.')
    } finally {
      setLoading(false)
    }
  }, [])

  // Refetched on every mount rather than cached: escalating in the investigate tab has to be
  // reflected here without a reload, and the table is small enough that a round trip is cheap.
  useEffect(() => {
    void load()
  }, [load])

  return (
    <div className="mx-auto flex w-full max-w-5xl flex-1 flex-col gap-4 overflow-y-auto px-6 py-6">
      <header className="flex items-center gap-3">
        <h1 className="text-[15px] font-medium text-foreground/90">Escalated flags</h1>
        {rows && (
          <span className="rounded-full bg-muted px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
            {rows.length}
          </span>
        )}
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
        Flags a human actually escalated, newest first — read from the audit trail, so this
        persists across reloads and shows escalations from every conversation, not just this one.
      </p>

      {error && (
        <div className="glass flex items-start gap-2.5 rounded-xl p-4 text-sm">
          <AlertCircle className="mt-0.5 size-4 shrink-0 text-destructive" />
          <div>
            <p className="font-medium text-foreground">Couldn't load escalations</p>
            <p className="mt-0.5 leading-relaxed text-muted-foreground">{error}</p>
          </div>
        </div>
      )}

      {!error && rows?.length === 0 && (
        <div className="glass flex flex-col items-center gap-2 rounded-xl px-6 py-14 text-center">
          <ShieldCheck className="size-6 text-muted-foreground" />
          <p className="text-sm font-medium text-foreground/90">Nothing escalated yet</p>
          <p className="max-w-sm text-xs leading-relaxed text-muted-foreground">
            Investigate a customer, then use Escalate on a flagged row. Escalations recorded there
            show up here.
          </p>
        </div>
      )}

      {!error && rows && rows.length > 0 && (
        <section className="glass overflow-hidden rounded-xl">
          <div className="scroll-thin overflow-x-auto">
            <table className="w-full min-w-[900px] border-collapse text-sm">
              <thead>
                <tr className="border-b border-border text-left text-[11px] tracking-wide text-muted-foreground uppercase">
                  <th className="px-5 py-2 font-medium">Escalated</th>
                  <th className="px-3 py-2 font-medium">Customer</th>
                  <th className="px-3 py-2 text-right font-medium">Amount</th>
                  <th className="px-3 py-2 font-medium">Risk</th>
                  <th className="px-3 py-2 font-medium">Pattern</th>
                  <th className="px-3 py-2 font-medium">Action</th>
                  <th className="px-5 py-2 font-medium">Raised by query</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr
                    className="border-b border-border/60 align-top transition-colors last:border-0 hover:bg-accent/40"
                    key={row.flag_id ?? `${row.customer_id}-${row.escalated_at}`}
                  >
                    <td className="px-5 py-3 text-xs whitespace-nowrap text-muted-foreground">
                      {when(row.escalated_at)}
                    </td>
                    <td className="px-3 py-3 font-mono text-xs text-foreground/90">
                      {row.customer_id}
                    </td>
                    <td className="px-3 py-3 text-right font-mono text-xs tabular-nums text-foreground">
                      {money.format(row.amount)}
                    </td>
                    <td className="px-3 py-3">
                      <span
                        className={cn(
                          'inline-flex rounded-full px-2 py-0.5 text-[11px] font-semibold tracking-wide',
                          RISK_CLASS[row.risk_level] ?? 'bg-muted text-muted-foreground',
                        )}
                      >
                        {row.risk_level}
                      </span>
                    </td>
                    <td className="px-3 py-3 font-mono text-xs text-muted-foreground">
                      {row.pattern_detected}
                    </td>
                    <td className="px-3 py-3 text-xs font-medium text-foreground/80">
                      {row.escalation_action}
                    </td>
                    <td className="max-w-[320px] px-5 py-3 text-xs leading-relaxed text-muted-foreground">
                      {row.query_text ?? '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </div>
  )
}
