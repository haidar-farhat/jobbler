# ADR 0002 — The model never sees or emits a selector

Status: **Accepted** · 2026-08-24

## Context

A browser agent has to say *which* element to act on. The usual options are all bad:

- **Pixel coordinates** (`click(842, 331)`) — brittle against scroll, zoom, and viewport size,
  and unverifiable: there is no way to check that (842, 331) is the button the model meant.
- **CSS selectors / XPath emitted by the model** — the model produces an arbitrary string that
  goes straight to `page.locator()`. A page that can influence the model can name *any*
  element on itself, including ones the model never saw.
- **Raw DOM in the prompt** — expensive, and mixes untrusted markup into the decision context.

The third-party content problem is the real driver. Job descriptions, company pages, and
listing text are **untrusted input written by strangers**. If the path from "text on a page"
to "selector passed to Playwright" is open, prompt injection is a direct route to arbitrary
page interaction.

## Decision

**The model never sees a CSS selector, XPath, or pixel coordinate, and never emits one.**

The observer enumerates interactive elements and assigns each an **opaque ref** — `e1`, `e2`,
`e17` — stamped onto the element as a `data-la-ref` attribute. It emits:

```json
{"ref": "e17", "role": "button", "name": "Easy Apply", "enabled": true, "required": false}
```

The `ref → Locator` map is held **privately inside `BrowserSession`**. A `Decision` may only
name a ref. The executor resolves `e17` against that map, and **a decision naming an unknown
ref is rejected before it reaches Playwright**.

Refs are **rebuilt from scratch on every observation**. A ref from a previous observation is
not in the current map, so stale references fail closed rather than acting on whatever
element happens to occupy that slot now.

## Consequences

**Good:**

- The reachable action set is bounded by construction: the model can only address elements the
  observer actually enumerated and showed it. Injection cannot widen it, because the model has
  no vocabulary for elements outside the map.
- Stale refs fail closed. Post-navigation, every ref is invalid until re-observation.
- Replay testing is trivial: a recorded `Observation` plus a `Decision` naming `e17` is a
  complete, browser-free test case.
- Prompts get smaller and cleaner — a ref table instead of a DOM dump.

**Costs:**

- Setting `data-la-ref` mutates the page. It is a non-visual attribute on existing elements,
  but it is a mutation, and a site could in principle detect it.
- One extra indirection to debug. The event log records the full element table per observation
  precisely so `e17` is always resolvable after the fact.
- Elements the observer fails to enumerate are unreachable. This is intended: unreachable is
  the correct failure mode, and the run pauses for the user rather than improvising.

## Alternatives rejected

- **Model-emitted selectors, validated against an allowlist** — the allowlist would have to be
  built from the observation anyway, which is this design with extra steps and a string-parsing
  attack surface.
- **Playwright's built-in ARIA snapshot refs** — close to this, but the map must be *ours* and
  private for the fail-closed guarantee to hold, and we need element metadata (`required`,
  `input_type`) that feeds field classification.
