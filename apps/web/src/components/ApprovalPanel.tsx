import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { api } from '../api'
import type { Approval } from '../types'

const CLASS_LABEL: Record<string, string> = {
  review_required: 'Needs your confirmation',
  never_autofill: 'Never filled automatically',
  safe_autofill: 'Verified profile fact',
}

function Card({ approval }: { approval: Approval }) {
  const queryClient = useQueryClient()
  const [value, setValue] = useState(approval.proposed_value ?? '')

  useEffect(() => setValue(approval.proposed_value ?? ''), [approval.id, approval.proposed_value])

  const resolve = useMutation({
    mutationFn: (approved: boolean) =>
      api.resolveApproval(approval.id, {
        approved,
        // Only send an edit when the value actually changed: an edited value gets a new
        // fingerprint, so it authorises exactly what is on screen.
        edited_value: value !== (approval.proposed_value ?? '') ? value : null,
      }),
    onSuccess: () => queryClient.invalidateQueries(),
  })

  const isSubmit = approval.action === 'submit'

  return (
    <div className="rounded border border-[var(--color-warn)]/40 bg-[var(--color-warn)]/5 p-4">
      <div className="flex items-baseline justify-between gap-2">
        <h3 className="font-semibold">
          {isSubmit ? 'Submit this application?' : approval.target_name}
        </h3>
        <span className="font-mono text-[10px] text-[var(--color-muted)]">
          {approval.policy_rule}
        </span>
      </div>

      <p className="mt-1 text-sm text-[var(--color-muted)]">{approval.reason}</p>

      {approval.field_class && (
        <p className="mt-2 text-xs text-[var(--color-warn)]">
          {CLASS_LABEL[approval.field_class] ?? approval.field_class}
        </p>
      )}

      {!isSubmit && approval.proposed_value !== null && (
        <label className="mt-3 block">
          <span className="text-xs text-[var(--color-muted)]">Value to enter</span>
          <input
            value={value}
            onChange={(e) => setValue(e.target.value)}
            className="mt-1 w-full rounded border border-[var(--color-edge)] bg-[var(--color-surface)] px-2 py-1.5 text-sm"
          />
        </label>
      )}

      <div className="mt-4 flex gap-2">
        <button
          onClick={() => resolve.mutate(true)}
          disabled={resolve.isPending}
          className="rounded bg-[var(--color-ok)] px-4 py-1.5 text-sm font-semibold text-black disabled:opacity-50"
        >
          Approve
        </button>
        <button
          onClick={() => resolve.mutate(false)}
          disabled={resolve.isPending}
          className="rounded border border-[var(--color-edge)] px-4 py-1.5 text-sm disabled:opacity-50"
        >
          Reject
        </button>
      </div>
    </div>
  )
}

export function ApprovalPanel({ runId }: { runId: string | null }) {
  const { data } = useQuery({
    queryKey: ['approvals'],
    queryFn: api.pendingApprovals,
    refetchInterval: 1500,
  })

  const pending = (data ?? []).filter((a) => !runId || a.run_id === runId)

  if (pending.length === 0) {
    return (
      <p className="p-4 text-sm text-[var(--color-muted)]">
        Nothing waiting on you. The agent stops here whenever a field is a negotiating
        position, or before any submit.
      </p>
    )
  }

  return (
    <div className="space-y-3 p-4">
      {pending.map((approval) => (
        <Card key={approval.id} approval={approval} />
      ))}
    </div>
  )
}
