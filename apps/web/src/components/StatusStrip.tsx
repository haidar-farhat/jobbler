import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api'

function Light({ ok, label, detail }: { ok: boolean; label: string; detail?: string }) {
  return (
    <div className="flex items-center gap-2">
      <span
        className={`inline-block h-2 w-2 rounded-full ${ok ? 'bg-[var(--color-ok)]' : 'bg-[var(--color-danger)]'}`}
      />
      <span className="text-xs tracking-wide text-[var(--color-muted)] uppercase">{label}</span>
      {detail && <span className="text-xs text-[var(--color-ink)]">{detail}</span>}
    </div>
  )
}

export function StatusStrip() {
  const queryClient = useQueryClient()
  const { data } = useQuery({
    queryKey: ['health'],
    queryFn: api.health,
    refetchInterval: 3000,
  })

  const stop = useMutation({
    mutationFn: () => api.killSwitch(),
    onSuccess: () => queryClient.invalidateQueries(),
  })
  const rearm = useMutation({
    mutationFn: () => api.rearm(),
    onSuccess: () => queryClient.invalidateQueries(),
  })

  const stopped = data?.safety.kill_switch.engaged ?? false

  return (
    <header className="border-b border-[var(--color-edge)] bg-[var(--color-panel)]">
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2 px-5 py-3">
        <span className="mr-2 font-semibold tracking-tight">LocalApply</span>

        <Light ok={data?.subsystems.database.ok ?? false} label="db" />
        <Light ok={data?.subsystems.redis.ok ?? false} label="redis" />
        <Light
          ok={data?.subsystems.browser.ok ?? false}
          label="browser"
          detail={
            data
              ? `${data.subsystems.browser.sessions}/${data.subsystems.browser.max_sessions}`
              : undefined
          }
        />
        <Light
          ok={data?.subsystems.ai.ok ?? false}
          label="ai"
          detail={data?.subsystems.ai.reasoner}
        />

        {/* The most important thing on the page: whether a submit would be real. */}
        {data && (
          <span
            className={`rounded px-2 py-0.5 text-xs font-semibold ${
              data.safety.dry_run
                ? 'bg-[var(--color-ok)]/15 text-[var(--color-ok)]'
                : 'bg-[var(--color-danger)]/15 text-[var(--color-danger)]'
            }`}
            title={
              data.safety.dry_run
                ? 'Submits are simulated and never actually clicked.'
                : 'Submits are REAL. Approving one sends a genuine application.'
            }
          >
            {data.safety.dry_run ? 'DRY RUN' : 'LIVE — SUBMITS ARE REAL'}
          </span>
        )}

        <div className="ml-auto flex items-center gap-3">
          {stopped && (
            <span className="text-xs text-[var(--color-danger)]">
              stopped: {data?.safety.kill_switch.reason}
            </span>
          )}
          {stopped ? (
            <button
              onClick={() => rearm.mutate()}
              className="rounded border border-[var(--color-edge)] px-3 py-1.5 text-xs font-semibold hover:bg-[var(--color-edge)]"
            >
              RE-ARM
            </button>
          ) : (
            <button
              onClick={() => stop.mutate()}
              className="rounded bg-[var(--color-danger)] px-3 py-1.5 text-xs font-bold text-white hover:brightness-110"
            >
              STOP ALL AUTOMATION
            </button>
          )}
        </div>
      </div>
    </header>
  )
}
