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

/** A flag a human escalated, joined back to the query that surfaced it (GET /escalations). */
export interface EscalatedFlag extends FlaggedItem {
  query_text: string | null
  query_timestamp: string | null
}

export interface EvidencePoint {
  label: string
  value: number
}

/** Context behind the flags, derived server-side. Absent when nothing was flagged. */
export interface Evidence {
  accounts: string[]
  daily_activity: EvidencePoint[]
  rule_mix: EvidencePoint[]
}

export interface AgentResult {
  query: string
  summary: string
  execution_summary: ExecutionSummary
  flagged_items: FlaggedItem[]
  evidence: Evidence | null
}

/** Aggregates over the whole audit trail (GET /stats), not just the current session. */
export interface StatPoint {
  label: string
  value: number
}

export interface DashboardStats {
  totals: { queries: number; flags: number; escalated: number }
  by_risk: StatPoint[]
  by_pattern: StatPoint[]
  top_accounts: StatPoint[]
  queries_by_day: StatPoint[]
  by_tool: StatPoint[]
  recent_queries: { label: string; intent: string | null; timestamp: string }[]
}
