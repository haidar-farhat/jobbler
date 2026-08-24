# ADR 0001 — Split the agent into Observe / Reason / Policy / Execute

Status: **Accepted** · 2026-08-24

## Context

The obvious way to build a browser agent is to give one multimodal model a screenshot and a
set of tools, and let it drive. It is also the way that produces a system nobody can test,
secure, or debug.

Three specific problems with the single-model design:

1. **Untestable.** If perception, judgment, and action live in one inference call, a wrong
   click could be a misread screenshot, a bad judgment, or a mis-executed tool call. There is
   no way to isolate which.
2. **Unsecurable.** If the model decides *and* acts, then any text on the page that
   manipulates the model's judgment reaches the browser directly. There is no place to stand
   between intent and effect.
3. **Non-deterministic where it must not be.** Clicking a button and typing into a field are
   mechanical operations. Routing them through a language model adds failure modes (wrong
   coordinates, hallucinated selectors) in exchange for nothing.

## Decision

Four layers with typed contracts between them:

```
Observation  →  Decision  →  PolicyVerdict  →  ActionResult
```

- **Observer** turns the live page into an `Observation`. It observes; it does not decide.
- **Reasoner** turns an `Observation` into a `Decision`. It is a **pure function of its
  inputs** — no browser handle, no DB session, no network. It proposes; it cannot act.
- **Policy engine** turns a `Decision` into a `PolicyVerdict`. **Plain code, no LLM.**
- **Executor** turns an allowed `Decision` into an `ActionResult` via deterministic
  Playwright calls.

## Consequences

**Good:**

- The reasoner can be replayed offline against recorded `Observation`s with no browser and no
  database. Model regressions become a diff on a fixture set.
- The policy engine is a pure function — hundreds of unit tests run in milliseconds, and it is
  the one place a security reviewer needs to read.
- Because policy is code and sits *outside* the model, no page content can weaken it. A
  prompt injection can at most produce a bad `Decision`, which policy then rejects.
- Swapping models is a one-line provider change; swapping Playwright for something else
  touches only the executor.

**Costs:**

- More types and more indirection than "give the model a tool." Four contracts to keep in
  sync instead of zero.
- The reasoner cannot opportunistically peek at the page mid-decision. It must ask for another
  observation, which costs a loop iteration. This is a deliberate trade: it is exactly the
  capability that would break the security boundary.

## Alternatives rejected

- **Single multimodal agent with tools** — rejected for the three reasons above.
- **Merging policy into the reasoner's system prompt** — a system prompt is a request, not a
  constraint. Page content competes with it on equal footing. Policy must be code.
- **Merging observer and reasoner** (one model call that both looks and decides) — cheaper by
  one inference, but destroys replay testing and puts raw page content in the same context
  that produces actions.
