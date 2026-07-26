import { ArrowUp, Square } from 'lucide-react'
import { useState, type KeyboardEvent } from 'react'
import { Textarea } from '../ui/textarea'
import { Button } from '../ui/button'
import { cn } from '../../lib/utils'

export default function ChatComposer({
  onSubmit,
  onStop,
  loading,
  initialValue = '',
  autoFocus = false,
  className,
}: {
  onSubmit: (text: string) => void
  /** Absent on the hero composer, where there is never a run to stop. */
  onStop?: () => void
  loading: boolean
  initialValue?: string
  autoFocus?: boolean
  className?: string
}) {
  const [value, setValue] = useState(initialValue)

  function submit() {
    const text = value.trim()
    if (!text || loading) return
    onSubmit(text)
    setValue('')
  }

  function handleKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      submit()
    }
  }

  return (
    <div className={cn('w-full', className)}>
      <div className="glass flex items-end gap-2 rounded-full p-2 pl-5 transition-shadow focus-within:ring-3 focus-within:ring-ring">
        <Textarea
          autoFocus={autoFocus}
          className="max-h-40 min-h-11 flex-1 resize-none border-0 bg-transparent px-1 py-2.5 shadow-none focus-visible:ring-0"
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="Ask about transactions, accounts, or patterns…"
          value={value}
        />
        {loading && onStop ? (
          <Button
            aria-label="Stop analysis"
            className="mb-1 size-9 shrink-0 rounded-full"
            onClick={onStop}
            size="icon"
            type="button"
            variant="outline"
          >
            <Square className="size-3.5 fill-current" />
          </Button>
        ) : (
          <Button
            aria-label="Send query"
            className="mb-1 size-9 shrink-0 rounded-full"
            disabled={loading || !value.trim()}
            onClick={submit}
            size="icon"
            type="button"
          >
            <ArrowUp className="size-4" />
          </Button>
        )}
      </div>
      <p className="mt-2.5 text-center text-[11px] text-muted-foreground">
        vigil can make mistakes. Verify flagged activity before filing a report.
      </p>
    </div>
  )
}
