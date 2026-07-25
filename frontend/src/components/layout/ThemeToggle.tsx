import { Monitor, Moon, Sun } from 'lucide-react'
import { useTheme, type ThemeMode } from '../../theme'
import { cn } from '../../lib/utils'

const OPTIONS: { mode: ThemeMode; icon: typeof Sun; label: string }[] = [
  { mode: 'light', icon: Sun, label: 'Light theme' },
  { mode: 'system', icon: Monitor, label: 'Match system theme' },
  { mode: 'dark', icon: Moon, label: 'Dark theme' },
]

export default function ThemeToggle({ collapsed = false }: { collapsed?: boolean }) {
  const { mode, setMode } = useTheme()

  if (collapsed) {
    const current = OPTIONS.findIndex((o) => o.mode === mode)
    const Icon = OPTIONS[current]?.icon ?? Monitor
    return (
      <button
        aria-label="Cycle theme"
        className="flex size-7 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
        onClick={() => setMode(OPTIONS[(current + 1) % OPTIONS.length].mode)}
        title={`Theme: ${mode}`}
        type="button"
      >
        <Icon className="size-3.5" />
      </button>
    )
  }

  return (
    <div className="inline-flex items-center gap-0.5 rounded-lg border border-border bg-muted/40 p-0.5">
      {OPTIONS.map(({ mode: m, icon: Icon, label }) => (
        <button
          key={m}
          aria-label={label}
          aria-pressed={mode === m}
          className={cn(
            'flex size-7 items-center justify-center rounded-md transition-colors duration-150',
            mode === m
              ? 'bg-card text-foreground shadow-sm'
              : 'text-muted-foreground hover:text-foreground',
          )}
          onClick={() => setMode(m)}
          type="button"
        >
          <Icon className="size-3.5" />
        </button>
      ))}
    </div>
  )
}
