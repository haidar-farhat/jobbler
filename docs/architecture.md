# LocalApply — Architecture

A local-first autonomous job-application workstation. Perception, reasoning, execution,
and user control are **four separate layers**, not one model with a browser attached.

## The fundamental loop

```
        ┌──────────────────────────────────────────┐
        │                                          │
        ▼                                          │
    OBSERVE  ──►  REASON  ──►  POLICY  ──►  EXECUTE
   (Playwright)   (LLM)      (plain code)  (Playwright)
   screenshot     Decision    Verdict       ActionResult
   + a11y tree
```

Each arrow is a **typed contract** (`Observation`, `Decision`, `PolicyVerdict`,
`ActionResult`). Each box is independently testable: the reasoner can be replayed against
recorded observations with no browser; the policy engine is a pure function unit-tested in
microseconds; the executor is driven by hand-built decisions in tests.

### Layer responsibilities, and what each may *not* do

| Layer | Input | Output | Forbidden |
|---|---|---|---|
| **Observer** `browser/observer.py` | live page | `Observation` | Making any decision |
| **Reasoner** `ai/reasoner.py` | `Observation` + profile | `Decision` | Touching the browser at all |
| **Policy** `policy/engine.py` | `Decision` + `Observation` + context | `PolicyVerdict` | Calling an LLM |
| **Executor** `browser/executor.py` | `Decision` | `ActionResult` | Interpreting intent |

The reasoner is a **pure function of its inputs** — it receives an `Observation` and returns
a `Decision`. It holds no browser handle, no database session, no network access. This is
what makes the security story work: a compromised or manipulated reasoner can only *propose*,
never *act*.

## The security boundary

The policy engine is **plain Python with no model in it**. It sits between the reasoner and
the executor, so nothing a web page says can alter it. Page content is data, never
instruction:

- All page text reaches the model wrapped in `<UNTRUSTED_WEB_CONTENT>…</UNTRUSTED_WEB_CONTENT>`.
- Every `Decision` is validated against an action allowlist *and* the known-ref set before
  it can reach Playwright.
- `SUBMIT` is a distinct action type and is **always** policy-gated — there is no code path
  that submits an application without an approval row.
- The kill switch is checked by the executor before *every* action, not once per loop.

See [ADR 0002](adr/0002-opaque-element-refs.md) for the element-ref indirection that closes
the prompt-injection path at the type level rather than by prompt instruction.

## Deployment shape

Measured constraints on the target machine (2026-08-24): RTX 5070 Laptop with **8151 MiB
VRAM**, **15.3 GB RAM**, Ryzen AI 7 350.

Two consequences drive the whole design:

**1. Models load sequentially, not concurrently.** A 7–8B Q4 reasoning model is ~5 GB; a
vision model is another ~4 GB. They cannot be co-resident. `ai/router.py` holds an exclusive
async lock and unloads the incumbent model on role change, with small embeddings pinned.
Model swaps cost 3–10 s and are therefore an **orchestration concern**, not an implementation
detail — the run loop batches work by model role to avoid thrashing.

**2. One process, not eight services.** The target layout in the design doc has eight
services. On 15 GB RAM that is premature. LocalApply is a **single FastAPI deployable whose
module boundaries match the service names 1:1**, so any module can be lifted into its own
process later without moving code between packages. Postgres and Redis run in Docker;
FastAPI and Playwright run natively on Windows, because WSL2 + Docker + Chromium + a 5 GB
model does not fit in 15 GB.

## Module map

```
services/api/localapply/
├─ contracts.py     # the four core types — the source of truth for all schemas
├─ config.py        # pydantic-settings, LA_* env vars
├─ db/              # SQLModel tables + Alembic migrations
├─ api/routes/      # health, profile, jobs, agent, events, approvals
├─ orchestrator/    # run_loop.py (the loop) + state_machine.py (application states)
├─ agents/          # discovery, analysis, application
├─ browser/         # session.py (owns the ref map), observer.py, executor.py
├─ ai/              # interface.py, router.py, reasoner.py, providers/
├─ policy/          # engine.py, rules.py, field_classifier.py
├─ profile/         # professional knowledge base
└─ events/          # append-only event bus + SSE
```

## State

`applications.state` is the deterministic state machine in `orchestrator/state_machine.py`.
Transitions are enforced in one place; agent code never writes the column directly.

```
DISCOVERED → PARSED → ANALYZED → SCORED → RECOMMENDED → USER_APPROVED
  → DOCUMENTS_GENERATING → READY_FOR_BROWSER → BROWSER_RUNNING → FORM_ANALYZED
  → SAFE_FIELDS_FILLED → REVIEW_REQUIRED → USER_APPROVED → SUBMITTING → SUBMITTED

any → BLOCKED → USER_INTERVENTION → (resume to prior state)
any → FAILED / CANCELLED
```

`agent_events` is append-only and is the single source for both the live SSE stream and
post-hoc replay. If you can read the event log, you can reconstruct exactly what the agent
saw, what it proposed, what policy said, and what actually happened.

## Current phase

**The loop, the knowledge base, document generation, and the job pipeline.** The complete
loop runs end to end against a local HTML fixture, driven either by `StubReasoner` (scripted,
deterministic) or by a local model through Ollama, with `ai/router.py` loading one large
model at a time under an exclusive lock. A CV is parsed into individually-approved facts —
proposed, never accepted on your behalf, and editable when the parser gets an entry wrong.
Tailored CVs and cover letters are assembled from accepted facts and refuse to render if any
line cites a fact you have not accepted. A job is added, scored against your accepted skills,
approved by you, given its own documents, and handed to the agent — with every step a
recorded state change.

Deliberately absent: automatic job-board discovery, the phone app, WireGuard. Each has an
interface stub so it drops in without refactoring.

### The job pipeline

A posting enters at `DISCOVERED`, and every step to `READY_FOR_BROWSER` is an explicit HTTP
request that writes one state:

```
POST /jobs                    → DISCOVERED (row default; creating a row is not a transition)
POST /jobs/{id}/ingest        → PARSED       paste the text, or read the URL you typed
POST /jobs/{id}/analyze       → ANALYZED → SCORED → RECOMMENDED
POST /jobs/{id}/approve       → USER_APPROVED    the human gate; needs confirm: true
POST /jobs/{id}/documents     → DOCUMENTS_GENERATING → READY_FOR_BROWSER
POST /jobs/{id}/apply         → hands over; the run loop owns the browser half
POST /jobs/{id}/unblock       → back to the stored resume_state, chosen by the server
POST /jobs/{id}/cancel        → CANCELLED
```

`applications.state` is written by **exactly two wrappers**, and a test greps for a third:
`jobs.pipeline.advance` before the browser, `RunManager._transition` after it. Each calls
`state_machine.transition()` first, and `advance()` has no tolerant flag — a pipeline step
asking for an illegal move is a 409, never a silent no-op. Every move also writes an
`audit_logs` row, which is what makes `GET /jobs/{id}` able to show a real history.

**Nothing in the pipeline is a model call.** Requirement extraction is a regular expression
over a fixed 84-name vocabulary and the score is arithmetic, so a posting cannot argue its
way to a higher score — and no automated path acts on the number anyway. Advancing past
`RECOMMENDED` takes a request from the user carrying an explicit confirmation.

Reading a posting from its URL is the one place the pipeline touches the network, and it is
never implicit. One navigation, of the URL the *user typed* — a URL found in page content is
never followed. No header, cookie, or user-agent tampering. Private and loopback addresses
are refused, before navigation and again after any redirect. A login wall or a CAPTCHA
stores nothing and parks the job for a person, because getting past one is their decision to
make in their own browser.
