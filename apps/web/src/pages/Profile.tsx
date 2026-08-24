import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api'
import type { ProfileFact } from '../types'

function FactRow({ fact }: { fact: ProfileFact }) {
  const queryClient = useQueryClient()
  const verify = useMutation({
    mutationFn: () => api.verifyFact(fact.id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['profile'] }),
  })

  return (
    <tr className="border-b border-[var(--color-edge)]/50">
      <td className="py-2 pr-4 font-mono text-xs text-[var(--color-muted)]">{fact.key}</td>
      <td className="py-2 pr-4">{fact.value}</td>
      <td className="py-2 pr-4 text-xs text-[var(--color-muted)]">{fact.source}</td>
      <td className="py-2">
        {fact.verified ? (
          <span className="text-xs text-[var(--color-ok)]">verified</span>
        ) : (
          <button
            onClick={() => verify.mutate()}
            className="rounded border border-[var(--color-edge)] px-2 py-0.5 text-xs hover:bg-[var(--color-edge)]"
          >
            Verify
          </button>
        )}
      </td>
    </tr>
  )
}

function Section({ title, note, facts }: { title: string; note: string; facts: ProfileFact[] }) {
  if (facts.length === 0) return null
  return (
    <section className="rounded border border-[var(--color-edge)] bg-[var(--color-panel)] p-4">
      <h2 className="font-semibold">{title}</h2>
      <p className="mt-1 mb-3 text-sm text-[var(--color-muted)]">{note}</p>
      <table className="w-full text-left text-sm">
        <tbody>
          {facts.map((fact) => (
            <FactRow key={fact.id} fact={fact} />
          ))}
        </tbody>
      </table>
    </section>
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

  const identity = data.facts.filter((f) => f.category !== 'answer')
  const drafts = data.facts.filter((f) => f.category === 'answer')

  return (
    <div className="space-y-4 p-5">
      <header className="rounded border border-[var(--color-edge)] bg-[var(--color-panel)] p-4">
        <h1 className="text-lg font-semibold">{data.profile.full_name}</h1>
        <p className="text-sm text-[var(--color-muted)]">{data.profile.email}</p>
      </header>

      <Section
        title="Verified facts"
        note="Only verified facts are ever entered into an application. Nothing here is written by the agent."
        facts={identity}
      />

      <Section
        title="Drafted answers"
        note="Proposals for fields that need your confirmation — salary, work authorisation, availability. The agent never enters these blind; each one stops the run for approval."
        facts={drafts}
      />
    </div>
  )
}
