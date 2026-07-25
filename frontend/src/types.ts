// Mirrors backend/schemas.py (frozen contract). Keep in sync; don't tighten risk_level.

export type EscalationAction = 'MONITOR' | 'REVIEW' | 'REPORT'

export interface SkippedTool {
  name: string
  reason: string
}

export interface ExecutionSummary {
  intent_detected: string
  filters_applied: Record<string, unknown>
  tools_invoked: string[]
  tools_skipped: SkippedTool[]
}

export interface FlaggedItem {
  flag_id: number | null
  customer_id: string
  transaction_id: string
  amount: number
  timestamp: string // ISO datetime
  risk_level: string // plain string on purpose: scale is still an open decision
  pattern_detected: string
  anomaly_score: number
  explanation: string
  escalation_action: EscalationAction
  escalated_at: string | null
}

export interface AgentResult {
  query: string
  summary: string
  execution_summary: ExecutionSummary
  flagged_items: FlaggedItem[]
}
