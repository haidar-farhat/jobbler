import type { AgentEvent, Approval, Health, ProfileResponse, RunSnapshot } from './types'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  })
  if (!response.ok) {
    const detail = await response.text()
    throw new Error(`${response.status} ${response.statusText} — ${detail}`)
  }
  return response.status === 204 ? (undefined as T) : ((await response.json()) as T)
}

export const api = {
  health: () => request<Health>('/health'),

  listRuns: () => request<RunSnapshot[]>('/agent/runs'),

  startRun: (body: { start_url: string; goal?: string; job_title?: string; company?: string }) =>
    request<RunSnapshot>('/agent/runs', { method: 'POST', body: JSON.stringify(body) }),

  pauseRun: (runId: string) => request<RunSnapshot>(`/agent/runs/${runId}/pause`, { method: 'POST' }),
  resumeRun: (runId: string) => request<RunSnapshot>(`/agent/runs/${runId}/resume`, { method: 'POST' }),
  stopRun: (runId: string) => request<RunSnapshot>(`/agent/runs/${runId}/stop`, { method: 'POST' }),

  /** STOP ALL AUTOMATION. Engages the global kill switch and unwinds every run. */
  killSwitch: (reason = 'Stopped from the dashboard') =>
    request<unknown>('/agent/kill-switch', { method: 'POST', body: JSON.stringify({ reason }) }),
  rearm: () => request<unknown>('/agent/kill-switch/reset', { method: 'POST' }),

  pendingApprovals: () => request<Approval[]>('/approvals?status=pending'),

  resolveApproval: (
    approvalId: string,
    body: { approved: boolean; edited_value?: string | null; source?: string },
  ) =>
    request<Approval>(`/approvals/${approvalId}/resolve`, {
      method: 'POST',
      body: JSON.stringify({ actor: 'user', source: 'web', ...body }),
    }),

  eventHistory: (runId: string, after = 0) =>
    request<AgentEvent[]>(`/agent/runs/${runId}/events/history?after=${after}`),

  profile: () => request<ProfileResponse>('/profile'),
  verifyFact: (factId: string) =>
    request<unknown>(`/profile/facts/${factId}/verify`, { method: 'POST' }),
}
