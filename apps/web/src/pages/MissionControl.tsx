import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { api } from '../api'
import { ApprovalPanel } from '../components/ApprovalPanel'
import { EventLog } from '../components/EventLog'
import { useEventStream } from '../useEventStream'

const FIXTURE_URL = 'http://localhost:8000/fixtures/job.html'

const STATUS_TONE: Record<string, string> = {
  running: 'text-[var(--color-ok)]',
  paused: 'text-[var(--color-warn)]',
  waiting_approval: 'text-[var(--color-warn)]',
  finished: 'text-[var(--color-accent)]',
  failed: 'text-[var(--color-danger)]',
  stopped: 'text-[var(--color-danger)]',
}

export function MissionControl() {
  const queryClient = useQueryClient()
  const [selected, setSelected] = useState<string | null>(null)
  const [url, setUrl] = useState(FIXTURE_URL)

  const { data: runs } = useQuery({
    queryKey: ['runs'],
    queryFn: api.listRuns,
    refetchInterval: 1500,
  })

  const activeId = selected ?? runs?.[runs.length - 1]?.run_id ?? null
  const run = runs?.find((r) => r.run_id === activeId) ?? null
  const events = useEventStream(activeId)

  const start = useMutation({
    mutationFn: () =>
      api.startRun({
        start_url: url,
        job_title: 'AI Engineer',
        company: 'Northwind Analytics',
      }),
    onSuccess: (created) => {
      setSelected(created.run_id)
      queryClient.invalidateQueries()
    },
  })

  const control = useMutation({
    mutationFn: ({ action }: { action: 'pause' | 'resume' | 'stop' }) => {
      if (!activeId) throw new Error('no run selected')
      return action === 'pause'
        ? api.pauseRun(activeId)
        : action === 'resume'
          ? api.resumeRun(activeId)
          : api.stopRun(activeId)
    },
    onSuccess: () => queryClient.invalidateQueries(),
  })

  /** The most recent screenshot the observer captured — the live view of the browser. */
  const screenshot = useMemo(() => {
    for (let i = events.length - 1; i >= 0; i--) {
      const id = events[i].payload?.screenshot_id
      if (typeof id === 'string') return `/screenshots/${id}.png`
    }
    return null
  }, [events])

  const currentPage = useMemo(() => {
    for (let i = events.length - 1; i >= 0; i--) {
      if (events[i].type === 'observation') return events[i]
    }
    return null
  }, [events])

  return (
    <div className="flex h-full flex-col gap-4 p-5">
      {/* Mission definition */}
      <section className="rounded border border-[var(--color-edge)] bg-[var(--color-panel)] p-4">
        <div className="flex flex-wrap items-end gap-3">
          <label className="min-w-80 flex-1">
            <span className="text-xs text-[var(--color-muted)]">Target URL</span>
            <input
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              className="mt-1 w-full rounded border border-[var(--color-edge)] bg-[var(--color-surface)] px-3 py-2 font-mono text-sm"
            />
          </label>
          <button
            onClick={() => start.mutate()}
            disabled={start.isPending}
            className="rounded bg-[var(--color-accent)] px-5 py-2 text-sm font-semibold text-black disabled:opacity-50"
          >
            Start run
          </button>
          <div className="flex gap-2">
            <button
              onClick={() => control.mutate({ action: 'pause' })}
              disabled={!run || run.status !== 'running'}
              className="rounded border border-[var(--color-edge)] px-4 py-2 text-sm disabled:opacity-40"
            >
              Pause
            </button>
            <button
              onClick={() => control.mutate({ action: 'resume' })}
              disabled={!run || run.status !== 'paused'}
              className="rounded border border-[var(--color-edge)] px-4 py-2 text-sm disabled:opacity-40"
            >
              Resume
            </button>
            <button
              onClick={() => control.mutate({ action: 'stop' })}
              disabled={!run || ['finished', 'failed', 'stopped'].includes(run.status)}
              className="rounded border border-[var(--color-edge)] px-4 py-2 text-sm disabled:opacity-40"
            >
              Stop
            </button>
          </div>
        </div>

        {start.error && (
          <p className="mt-3 text-sm text-[var(--color-danger)]">{String(start.error)}</p>
        )}

        {run && (
          <dl className="mt-4 grid grid-cols-2 gap-x-8 gap-y-2 text-sm sm:grid-cols-4">
            <div>
              <dt className="text-xs text-[var(--color-muted)]">Status</dt>
              <dd className={STATUS_TONE[run.status] ?? ''}>{run.status}</dd>
            </div>
            <div>
              <dt className="text-xs text-[var(--color-muted)]">Application state</dt>
              <dd className="font-mono text-xs">{run.state}</dd>
            </div>
            <div>
              <dt className="text-xs text-[var(--color-muted)]">Actions</dt>
              <dd>
                {run.actions_executed} / {run.max_actions}
              </dd>
            </div>
            <div>
              <dt className="text-xs text-[var(--color-muted)]">Page</dt>
              <dd className="truncate font-mono text-xs">
                {(currentPage?.payload?.page_kind as string) ?? '—'}
              </dd>
            </div>
          </dl>
        )}
      </section>

      {/* Live browser view + approvals */}
      <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 lg:grid-cols-2">
        <section className="flex min-h-0 flex-col rounded border border-[var(--color-edge)] bg-[var(--color-panel)]">
          <h2 className="border-b border-[var(--color-edge)] px-4 py-2 text-xs tracking-wide text-[var(--color-muted)] uppercase">
            Live browser
          </h2>
          <div className="min-h-0 flex-1 overflow-auto p-3">
            {screenshot ? (
              <img
                src={screenshot}
                alt="What the agent currently sees"
                className="w-full rounded border border-[var(--color-edge)]"
              />
            ) : (
              <p className="text-sm text-[var(--color-muted)]">
                No screenshot yet. The observer captures one per loop iteration.
              </p>
            )}
          </div>
        </section>

        <section className="flex min-h-0 flex-col rounded border border-[var(--color-edge)] bg-[var(--color-panel)]">
          <h2 className="border-b border-[var(--color-edge)] px-4 py-2 text-xs tracking-wide text-[var(--color-muted)] uppercase">
            Waiting on you
          </h2>
          <div className="min-h-0 flex-1 overflow-auto">
            <ApprovalPanel runId={activeId} />
          </div>
        </section>
      </div>

      {/* The append-only stream */}
      <section className="flex h-72 min-h-0 flex-col rounded border border-[var(--color-edge)] bg-[var(--color-panel)]">
        <h2 className="border-b border-[var(--color-edge)] px-4 py-2 text-xs tracking-wide text-[var(--color-muted)] uppercase">
          Agent event stream
        </h2>
        <div className="min-h-0 flex-1">
          <EventLog events={events} />
        </div>
      </section>
    </div>
  )
}
