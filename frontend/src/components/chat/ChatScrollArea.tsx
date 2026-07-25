import { ArrowDown } from 'lucide-react'
import type { ReactNode } from 'react'
import { StickToBottom, useStickToBottomContext } from 'use-stick-to-bottom'
import { cn } from '../../lib/utils'

function ScrollToBottomButton() {
  const { isAtBottom, scrollToBottom } = useStickToBottomContext()

  if (isAtBottom) return null

  return (
    <button
      className={cn(
        'absolute bottom-4 left-1/2 flex -translate-x-1/2 items-center gap-1.5 rounded-full border border-border',
        'bg-card px-3 py-1.5 text-xs font-medium text-foreground shadow-md animate-fade-up',
        'transition-colors hover:bg-accent',
      )}
      onClick={() => scrollToBottom()}
      type="button"
    >
      <ArrowDown className="size-3.5" />
      New activity
    </button>
  )
}

export default function ChatScrollArea({ children }: { children: ReactNode }) {
  return (
    <StickToBottom
      className="scroll-thin relative min-h-0 flex-1"
      initial="smooth"
      resize="smooth"
      role="log"
    >
      <StickToBottom.Content className="mx-auto flex w-full max-w-3xl flex-col gap-8 px-6 py-8">
        {children}
      </StickToBottom.Content>
      <ScrollToBottomButton />
    </StickToBottom>
  )
}
