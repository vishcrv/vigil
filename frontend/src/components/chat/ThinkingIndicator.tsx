const DOTS = [0, 1, 2]

export default function ThinkingIndicator({ label = 'Analyzing transactions' }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 text-sm text-muted-foreground">
      <span>{label}</span>
      <span className="flex items-center gap-1">
        {DOTS.map((i) => (
          <span
            key={i}
            className="size-1.5 animate-pulse-soft rounded-full bg-current"
            style={{ animationDelay: `${i * 0.2}s` }}
          />
        ))}
      </span>
    </div>
  )
}
