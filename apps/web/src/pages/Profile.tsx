import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api'
import type { ProfileFact } from '../types'

function ProposalRow({ fact }: { fact: ProfileFact }) {
  const queryClient = useQueryClient()
  const decide = useMutation({
    mutationFn: (accept: boolean) =>
      accept ? api.acceptFact(fact.id) : api.rejectFact(fact.id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['profile'] }),
  })

  return (
    <tr className="border-b border-[var(--color-edge)]/50">
      <td className="py-2 pr-3">
        <span
          className={`mr-2 rounded px-1.5 py-0.5 text-[10px] ${
            fact.supersedes_id
              ? 'bg-[var(--color-warn)]/15 text-[var(--color-warn)]'
              : 'bg-[var(--color-accent)]/15 text-[var(--color-accent)]'
          }`}
        >
          {fact.supersedes_id ? 'replaces' : 'new'}
        </span>
        <span className="text-xs text-[var(--color-muted)]">
          {fact.category} · {fact.key}
        </span>
      </td>
      <td className="py-2 pr-3">{fact.value}</td>
      <td className="py-2 pr-3 font-mono text-xs text-[var(--color-muted)]">
        {fact.confidence.toFixed(2)}
      </td>
      <td className="py-2 pr-3 text-xs text-[var(--color-muted)]">{fact.evidence}</td>
      <td className="py-2 whitespace-nowrap">
        <button
          onClick={() => decide.mutate(true)}
          disabled={decide.isPending}
          className="rounded bg-[var(--color-ok)] px-3 py-1 text-xs font-semibold text-black disabled:opacity-50"
        >
          Accept
        </button>{' '}
        <button
          onClick={() => decide.mutate(false)}
          disabled={decide.isPending}
          className="rounded border border-[var(--color-edge)] px-3 py-1 text-xs disabled:opacity-50"
        >
          Decline
        </button>
      </td>
    </tr>
  )
}

export function Profile() {
  const { data } = useQuery({ queryKey: ['profile'], queryFn: api.profile })

  if (!data?.profile) {
    return (
      <div className="p-5">
        <p className="text-sm text-[var(--color-muted)]">
          No profile yet. Run{' '}
          <code className="font-mono text-[var(--color-ink)]">
            python scripts/dev_bootstrap.py
          </code>{' '}
          to seed one.
        </p>
      </div>
    )
  }

  const accepted = data.facts.filter((f) => f.status === 'accepted')
  const proposed = data.facts.filter((f) => f.status === 'proposed')
  const byCategory = accepted.reduce<Record<string, ProfileFact[]>>((acc, fact) => {
    ;(acc[fact.category] ||= []).push(fact)
    return acc
  }, {})

  return (
    <div className="space-y-4 p-5">
      <header className="rounded border border-[var(--color-edge)] bg-[var(--color-panel)] p-4">
        <h1 className="text-lg font-semibold">{data.profile.full_name}</h1>
        <p className="text-sm text-[var(--color-muted)]">{data.profile.email}</p>
      </header>

      {proposed.length > 0 && (
        <section className="rounded border border-[var(--color-warn)]/40 bg-[var(--color-panel)] p-4">
          <h2 className="font-semibold">Proposed facts — waiting on you</h2>
          <p className="mt-1 mb-3 text-sm text-[var(--color-muted)]">
            Extracted from a document. None of these are used by the agent until you accept
            them, and each one shows the line it came from so you can check it.
          </p>
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="text-xs text-[var(--color-muted)] uppercase">
                <th className="pb-1">What</th>
                <th className="pb-1">Value</th>
                <th className="pb-1">Confidence</th>
                <th className="pb-1">Source line</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {proposed.map((fact) => (
                <ProposalRow key={fact.id} fact={fact} />
              ))}
            </tbody>
          </table>
        </section>
      )}

      <section className="rounded border border-[var(--color-edge)] bg-[var(--color-panel)] p-4">
        <h2 className="font-semibold">Accepted facts — what the agent may use</h2>
        <p className="mt-1 mb-3 text-sm text-[var(--color-muted)]">
          Only these reach an application. Facts in the <code>answer</code> category are
          drafts for questions that still stop the run for your confirmation.
        </p>
        {accepted.length === 0 ? (
          <p className="text-sm text-[var(--color-muted)]">Nothing accepted yet.</p>
        ) : (
          Object.keys(byCategory)
            .sort()
            .map((category) => (
              <div key={category} className="mt-4">
                <h3 className="text-xs tracking-wide text-[var(--color-muted)] uppercase">
                  {category} ({byCategory[category].length})
                </h3>
                <table className="mt-1 w-full text-left text-sm">
                  <tbody>
                    {byCategory[category].map((fact) => (
                      <tr key={fact.id} className="border-b border-[var(--color-edge)]/50">
                        <td className="w-48 py-1.5 pr-4 font-mono text-xs text-[var(--color-muted)]">
                          {fact.key}
                        </td>
                        <td className="py-1.5">{fact.value}</td>
                        <td className="py-1.5 text-xs text-[var(--color-muted)]">
                          {fact.source}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ))
        )}
      </section>
    </div>
  )
}
