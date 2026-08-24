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

**Walking skeleton.** The complete loop runs end to end against a local HTML fixture, driven
by `StubReasoner` (scripted, deterministic). Real models and real job sites slot in behind
interfaces that are proven working first. Deliberately absent: CV parsing, real job-board
discovery, local inference, the phone app, WireGuard. Each has an interface stub so it drops
in without refactoring.
