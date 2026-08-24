# ADR 0003 — A model may rephrase a fact, never author a claim

Status: **Accepted** · 2026-08-24

## Context

Phase 3 generates CVs and cover letters deterministically: each document item carries the
ids of the accepted facts backing it, and `assert_grounded` refuses to render anything that
cites nothing or cites a fact you have not accepted.

That check is sufficient while code writes the prose, because the code only ever emits a
fact's own text. It stops being sufficient the moment a model writes the prose. A model handed
five facts can still produce a sentence mentioning a sixth thing, and `assert_grounded` would
not notice: the *item* still cites five real facts, while the *sentence* now claims something
none of them support.

The stakes are not abstract. A hallucinated skill on a CV is a false statement to an employer,
sent under the user's name, discovered at interview.

## Decision

**The model is a rewriter, not an author.**

The `DocumentPlan` is built deterministically and is grounded by construction. Only then is
the model invoked, one item at a time, with a single instruction: say this same thing better.
It receives one line and returns one line.

It therefore *cannot*:

- add an item, because it is never asked for one;
- add a section, for the same reason;
- change `fact_ids`, because it never sees them.

That reduces the attack surface to one failure: the rewritten sentence saying more than its
source. `claims.check_claims` targets exactly that, comparing the rewrite against the text of
the facts it was built from and flagging any named technology or invented figure the source
does not contain. A flagged rewrite is discarded and the original wording kept.

## Consequences

**Good:**

- The grounding guarantee survives the introduction of a model, unchanged and re-asserted
  after rewriting.
- A hallucinating model degrades the *prose* and never the *truth* — the worst case is a
  document that reads exactly as well as the deterministic one.
- Rejections are surfaced to the user, so being overruled is visible rather than silent.
- The same shape works for any future model, local or hosted.

**Costs:**

- One model call per line rather than one per document, so polishing is slower and capped at
  a few items.
- The result is constrained. A model that could write a genuinely better letter by drawing a
  connection between two facts is not allowed to, because "drawing a connection" and
  "inventing a claim" are not separable from outside.
- `check_claims` matches a known technology vocabulary and numeric patterns. It cannot catch
  every embellishment — "I thrive in fast-paced teams" is unfalsifiable and passes. This is a
  mitigation, not a proof, and is documented as such.

## Alternatives rejected

- **Let the model write the document from the facts.** Simplest and produces the best prose,
  and is exactly the failure mode this project exists to avoid. There would be no structural
  difference between a fact and an invention in the output.
- **Ask the model to cite its facts.** Self-reported provenance from the component whose
  honesty is in question. A model that invents a claim will happily invent a citation.
- **Post-hoc review only.** Relies on the user catching a plausible-sounding false line in a
  document they asked to be written for them, which is precisely when attention is lowest.
