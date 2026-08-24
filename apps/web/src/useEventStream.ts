import { useEffect, useRef, useState } from 'react'
import type { AgentEvent } from './types'

/**
 * Subscribe to a run's server-sent event stream.
 *
 * The backlog arrives first, so opening the dashboard mid-run catches up rather than
 * starting blank. Events are keyed by `seq` on the way in, because a reconnect replays the
 * backlog and would otherwise duplicate everything already on screen.
 */
export function useEventStream(runId: string | null) {
  const [events, setEvents] = useState<AgentEvent[]>([])
  const seen = useRef<Set<number>>(new Set())

  useEffect(() => {
    setEvents([])
    seen.current = new Set()
    if (!runId) return

    const source = new EventSource(`/agent/runs/${runId}/events`)

    const handle = (raw: MessageEvent) => {
      if (!raw.data || raw.data === '{}') return
      const event = JSON.parse(raw.data) as AgentEvent
      if (seen.current.has(event.seq)) return
      seen.current.add(event.seq)
      setEvents((current) => [...current, event])
    }

    // The server names each event by its type, so a generic listener is not enough.
    const types = [
      'run_started', 'observation', 'decision', 'policy_verdict', 'action_result',
      'approval_requested', 'approval_resolved', 'state_changed', 'run_paused',
      'run_resumed', 'run_finished', 'run_failed', 'kill_switch', 'log',
    ]
    for (const type of types) source.addEventListener(type, handle)
    source.onmessage = handle

    return () => {
      for (const type of types) source.removeEventListener(type, handle)
      source.close()
    }
  }, [runId])

  return events
}
