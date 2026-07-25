import type { ReactNode } from 'react'

export function UserMessage({ children }: { children: ReactNode }) {
  return (
    <div className="flex animate-fade-up justify-end">
      <div className="max-w-[80%] rounded-2xl rounded-br-md bg-primary px-4 py-2.5 text-sm text-primary-foreground">
        {children}
      </div>
    </div>
  )
}

export function AssistantMessage({ children }: { children: ReactNode }) {
  return <div className="flex animate-fade-up flex-col gap-5">{children}</div>
}
