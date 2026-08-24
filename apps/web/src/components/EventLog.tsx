import { useEffect, useRef } from 'react'
import type { AgentEvent, EventType } from '../types'

/** Each layer of the loop gets its own colour, so the Observe -> Reason -> Policy -> Execute
 *  rhythm is visible at a glance in the stream. */
const TONE: Record<EventType, string> = {
  run_started: 'text-[var(--color-accent)]',
  observation: 'text-[var(--color-muted)]',
  decision: 'text-[var(--color-ink)]',
  policy_verdict: 'text-[var(--color-warn)]',
  action_result: 'text-[var(--color-ok)]',
  approval_requested: 'text-[var(--color-warn)]',
  approval_resolved: 'text-[var(--color-accent)]',
  state_changed: 'text-[var(--color-accent)]',
  run_paused: 'text-[var(--color-warn)]',
  run_resumed: 'text-[var(--color-accent)]',
  run_finished: 'text-[var(--color-ok)]',
  run_failed: 'text-[var(--color-danger)]',
  kill_switch: 'text-[var(--color-danger)]',
  log: 'text-[var(--color-muted)]',
}

function time(ts: string) {
  return new Date(ts).toLocaleTimeString([], { hour12: false })
}

export function EventLog({ events }: { events: AgentEvent[] }) {
  const bottom = useRef<HTMLDivElement>(null)

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: 'smooth' })
  }, [events.length])

  if (events.length === 0) {
    return (
      <p className="p-4 text-sm text-[var(--color-muted)]">
        No events yet. Start a run to watch the agent work.
      </p>
    )
  }

  return (
    <div className="h-full overflow-y-auto px-4 py-3 font-mono text-xs leading-relaxed">
      {events.map((event) => (
        <div key={event.seq} className="flex gap-3 border-b border-[var(--color-edge)]/40 py-1">
          <span className="shrink-0 text-[var(--color-muted)]">{time(event.ts)}</span>
          <span className="w-24 shrink-0 text-[var(--color-muted)]">{event.agent}</span>
          <span className={`w-40 shrink-0 ${TONE[event.type]}`}>{event.type}</span>
          <span className="min-w-0 flex-1 break-words">{event.message}</span>
        </div>
      ))}
      <div ref={bottom} />
    </div>
  )
}
