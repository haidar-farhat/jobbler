/**
 * Mirrors services/api/localapply/contracts.py.
 *
 * Regenerate with `python scripts/export_schemas.py` once the schema export lands in the
 * build; until then keep this file in step with contracts.py by hand.
 */

export type EventType =
  | 'run_started'
  | 'observation'
  | 'decision'
  | 'policy_verdict'
  | 'action_result'
  | 'approval_requested'
  | 'approval_resolved'
  | 'state_changed'
  | 'run_paused'
  | 'run_resumed'
  | 'run_finished'
  | 'run_failed'
  | 'kill_switch'
  | 'log'

export interface AgentEvent {
  event_id: string
  run_id: string
  seq: number
  type: EventType
  agent: string
  message: string
  payload: Record<string, unknown>
  ts: string
}

export interface RunSnapshot {
  run_id: string
  application_id: string | null
  status: 'running' | 'paused' | 'waiting_approval' | 'finished' | 'failed' | 'stopped'
  state: string
  goal: string
  start_url: string
  actions_executed: number
  max_actions: number
  error: string | null
  pending_approval: string | null
}

export interface Approval {
  id: string
  run_id: string
  action: string
  target_ref: string | null
  target_name: string | null
  proposed_value: string | null
  reason: string
  policy_rule: string
  field_class: string | null
  status: 'pending' | 'approved' | 'rejected'
  created_at: string
}

export interface Health {
  status: string
  subsystems: {
    database: { ok: boolean; error: string | null }
    redis: { ok: boolean; error: string | null }
    browser: { ok: boolean; sessions: number; max_sessions: number }
    ai: { ok: boolean; reasoner: string }
  }
  safety: {
    kill_switch: { engaged: boolean; reason: string | null; engaged_at: string | null }
    dry_run: boolean
  }
  runs: { active: RunSnapshot[]; waiting_approval: RunSnapshot[]; total: number }
}

export type FactStatus = 'accepted' | 'proposed' | 'rejected' | 'superseded'

export interface ProfileFact {
  id: string
  key: string
  value: string
  category: string
  source: string
  /** Only 'accepted' facts are ever entered into an application. */
  status: FactStatus
  confidence: number
  evidence: string | null
  document_id: string | null
  supersedes_id: string | null
}

export interface ProfileResponse {
  profile: { id: string; full_name: string; email: string } | null
  facts: ProfileFact[]
}
