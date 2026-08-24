import { useState } from 'react'
import { StatusStrip } from './components/StatusStrip'
import { MissionControl } from './pages/MissionControl'
import { Profile } from './pages/Profile'

type Tab = 'mission' | 'profile'

export function App() {
  const [tab, setTab] = useState<Tab>('mission')

  return (
    <div className="flex h-screen flex-col">
      <StatusStrip />

      <nav className="flex gap-1 border-b border-[var(--color-edge)] bg-[var(--color-panel)] px-5">
        {(
          [
            ['mission', 'Mission Control'],
            ['profile', 'Profile'],
          ] as const
        ).map(([key, label]) => (
          <button
            key={key}
            onClick={() => setTab(key)}
            className={`border-b-2 px-4 py-2 text-sm ${
              tab === key
                ? 'border-[var(--color-accent)] text-[var(--color-ink)]'
                : 'border-transparent text-[var(--color-muted)] hover:text-[var(--color-ink)]'
            }`}
          >
            {label}
          </button>
        ))}
      </nav>

      <main className="min-h-0 flex-1 overflow-auto">
        {tab === 'mission' ? <MissionControl /> : <Profile />}
      </main>
    </div>
  )
}
