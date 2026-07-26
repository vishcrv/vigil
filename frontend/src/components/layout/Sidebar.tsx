import { useEffect, useState } from 'react'
import {
  AlertTriangle,
  Flag,
  MessageSquareText,
  PanelLeftClose,
  PanelLeftOpen,
  ShieldHalf,
  Trash2,
} from 'lucide-react'
import type { ChatEntry } from '../../App'
import ThemeToggle from './ThemeToggle'
import { cn } from '../../lib/utils'

const STORAGE_KEY = 'vigil-sidebar-collapsed'

function StatTile({
  icon: Icon,
  label,
  value,
  collapsed,
}: {
  icon: typeof Flag
  label: string
  value: number
  collapsed: boolean
}) {
  if (collapsed) {
    return (
      <div
        className="glass-chip flex flex-col items-center gap-0.5 rounded-lg py-2 text-muted-foreground"
        title={`${label}: ${value}`}
      >
        <Icon className="size-3.5" />
        <span className="font-mono text-xs font-semibold tabular-nums text-foreground">
          {value}
        </span>
      </div>
    )
  }

  return (
    <div className="glass-chip flex flex-col gap-1 rounded-lg px-3 py-2.5">
      <div className="flex items-center gap-1.5 text-muted-foreground">
        <Icon className="size-3.5" />
        <span className="text-[11px] font-medium tracking-wide uppercase">{label}</span>
      </div>
      <span className="font-mono text-lg font-semibold tabular-nums text-foreground">{value}</span>
    </div>
  )
}

export default function Sidebar({
  entries,
  onSelectQuery,
  onClear,
}: {
  entries: ChatEntry[]
  // Takes the entry id, not the query text: clicking a past query used to re-seed the composer
  // and re-run it, which spends provider quota to reproduce an answer already on screen.
  onSelectQuery: (entryId: string) => void
  onClear: () => void
}) {
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem(STORAGE_KEY) === '1')

  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, collapsed ? '1' : '0')
  }, [collapsed])

  const results = entries.map((e) => e.result).filter((r) => r !== null)
  const flagged = results.flatMap((r) => r.flagged_items)
  // HIGH *and* CRITICAL: both are "high risk" to an analyst, and counting only HIGH left the
  // most severe flags out of the tally entirely.
  const highRisk = flagged.filter(
    (f) => f.risk_level === 'HIGH' || f.risk_level === 'CRITICAL',
  ).length

  return (
    <aside
      className={cn(
        'glass relative z-10 flex h-full shrink-0 flex-col rounded-none',
        'transition-[width] duration-300 ease-out',
        collapsed ? 'w-16' : 'w-72',
      )}
    >
      <div
        className={cn(
          'flex items-center gap-2.5 px-4 py-5',
          collapsed && 'flex-col gap-3 px-2',
        )}
      >
        <div className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-primary text-primary-foreground shadow-sm">
          <ShieldHalf className="size-4.5" />
        </div>
        {!collapsed && (
          <div className="min-w-0 leading-tight">
            <div className="truncate text-sm font-semibold tracking-tight text-foreground">
              vigil
            </div>
            <div className="truncate text-[11px] text-muted-foreground">AML detection agent</div>
          </div>
        )}
        <button
          aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          className={cn(
            'flex size-6 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground',
            !collapsed && 'ml-auto',
          )}
          onClick={() => setCollapsed((c) => !c)}
          type="button"
        >
          {collapsed ? <PanelLeftOpen className="size-3.5" /> : <PanelLeftClose className="size-3.5" />}
        </button>
      </div>

      <div className={cn('grid gap-2 px-4', collapsed ? 'grid-cols-1 px-2' : 'grid-cols-3')}>
        <StatTile collapsed={collapsed} icon={MessageSquareText} label="Queries" value={entries.length} />
        <StatTile collapsed={collapsed} icon={Flag} label="Flagged" value={flagged.length} />
        <StatTile collapsed={collapsed} icon={AlertTriangle} label="High risk" value={highRisk} />
      </div>

      {!collapsed && (
        <div className="mt-6 flex min-h-0 flex-1 flex-col px-4">
          <div className="mb-2 flex items-center gap-2">
            <span className="text-[11px] font-medium tracking-wide text-muted-foreground uppercase">
              Recent queries
            </span>
            {entries.length > 0 && (
              <button
                className="ml-auto inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[11px] text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
                onClick={onClear}
                title="Clear this conversation"
                type="button"
              >
                <Trash2 className="size-3" />
                Clear
              </button>
            )}
          </div>

          {entries.length === 0 ? (
            <p className="text-xs leading-relaxed text-muted-foreground/70">
              Queries you run this session will show up here.
            </p>
          ) : (
            <ul className="scroll-thin -mx-1 flex-1 space-y-0.5 overflow-y-auto px-1">
              {[...entries].reverse().map((entry) => (
                <li key={entry.id}>
                  <button
                    className="w-full truncate rounded-md px-2 py-1.5 text-left text-xs text-foreground/80 transition-colors duration-150 hover:bg-accent hover:text-foreground"
                    onClick={() => onSelectQuery(entry.id)}
                    title={entry.query}
                    type="button"
                  >
                    {entry.query}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {collapsed && <div className="min-h-0 flex-1" />}

      <div
        className={cn(
          'flex items-center justify-between border-t border-border/60 px-4 py-3',
          collapsed && 'flex-col gap-2 px-2',
        )}
      >
        {!collapsed && <span className="text-[11px] text-muted-foreground">Theme</span>}
        <ThemeToggle collapsed={collapsed} />
      </div>
    </aside>
  )
}
