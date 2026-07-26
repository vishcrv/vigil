const DOTS = [0, 1, 2]

// Names the tool currently running rather than showing an undifferentiated spinner: a run is
// several sequential calls over 5-15 seconds, and "Running anomaly" is the difference between
// looking stuck and looking like it is working.
const TOOL_LABEL: Record<string, string> = {
  eda: 'Querying the dataset',
  feature_eng: 'Building features',
  anomaly: 'Running anomaly detection',
  risk: 'Classifying risk',
  explain: 'Writing the explanation',
}

export default function ThinkingIndicator({
  label = 'Analyzing transactions',
  tool = null,
}: {
  label?: string
  tool?: string | null
}) {
  return (
    <div className="flex items-center gap-2 text-sm text-muted-foreground">
      <span>{tool ? (TOOL_LABEL[tool] ?? `Running ${tool}`) : label}</span>
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
