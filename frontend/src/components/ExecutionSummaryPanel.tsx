import { Check, X } from 'lucide-react'
import type { ExecutionSummary } from '../types'
import { Badge } from './ui/badge'
import { cn } from '../lib/utils'

export default function ExecutionSummaryPanel({
  summary,
  narrative,
}: {
  summary: ExecutionSummary
  narrative: string
}) {
  const filters = Object.entries(summary.filters_applied)
  const steps = [
    ...summary.tools_invoked.map((name) => ({ name, invoked: true, reason: undefined as string | undefined })),
    ...summary.tools_skipped.map((t) => ({ name: t.name, invoked: false, reason: t.reason })),
  ]

  return (
    <section className="glass rounded-xl p-5">
      <div className="mb-3 flex items-center gap-2">
        <span className="text-[11px] font-medium tracking-wide text-muted-foreground uppercase">
          Decision flow
        </span>
        <Badge className="font-mono text-[10px]" variant="outline">
          {summary.intent_detected}
        </Badge>
      </div>

      <p className="text-[15px] leading-relaxed text-foreground/90">{narrative}</p>

      <div className="mt-5 grid gap-6 border-t border-border pt-4 sm:grid-cols-[minmax(0,1fr)_minmax(0,1.3fr)]">
        <div>
          <h3 className="mb-2 text-[11px] font-medium tracking-wide text-muted-foreground uppercase">
            Filters applied
          </h3>
          <dl className="space-y-1.5">
            {filters.map(([key, value]) => (
              <div className="flex items-baseline gap-2 text-xs" key={key}>
                <dt className="min-w-[92px] shrink-0 text-muted-foreground">{key}</dt>
                <dd className="truncate font-mono text-foreground/90">
                  {Array.isArray(value) ? value.join(' → ') : String(value)}
                </dd>
              </div>
            ))}
          </dl>
        </div>

        <div>
          <h3 className="mb-2 text-[11px] font-medium tracking-wide text-muted-foreground uppercase">
            Tool execution
          </h3>
          <ol className="space-y-2.5">
            {steps.map((step, i) => (
              <li
                className="flex animate-fade-up items-start gap-2.5 text-xs"
                key={step.name}
                style={{ animationDelay: `${i * 60}ms` }}
              >
                <span
                  className={cn(
                    'mt-0.5 flex size-4 shrink-0 items-center justify-center rounded-full',
                    step.invoked
                      ? 'bg-risk-low-bg text-risk-low'
                      : 'bg-muted text-muted-foreground',
                  )}
                >
                  {step.invoked ? <Check className="size-2.5" /> : <X className="size-2.5" />}
                </span>
                <div className="min-w-0">
                  <span
                    className={cn(
                      'font-mono',
                      step.invoked ? 'text-foreground' : 'text-muted-foreground line-through',
                    )}
                  >
                    {step.name}
                  </span>
                  {step.reason && (
                    <p className="mt-0.5 leading-snug text-muted-foreground">{step.reason}</p>
                  )}
                </div>
              </li>
            ))}
          </ol>
        </div>
      </div>
    </section>
  )
}
