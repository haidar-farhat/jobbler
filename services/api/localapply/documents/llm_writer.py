"""Let a model improve the prose of an already-grounded document.

The important structural choice: **the model is a rewriter, not an author.**

A `DocumentPlan` is built deterministically from accepted facts, so it is grounded by
construction. The model is then handed one item at a time and asked to say the same thing
better. It may change `text`; it can never change `fact_ids`, add an item, or add a section,
because it is never given the opportunity to -- the plan's structure is fixed before it is
called.

So the failure modes shrink to one: the rewritten sentence saying more than the fact did.
`claims.assert_supported` checks precisely that, and anything it flags falls back to the
deterministic wording. A model that hallucinates degrades the prose, never the truth.
"""

from __future__ import annotations

import logging

from ..ai.interface import ProviderUnavailable
from .claims import UnsupportedClaim, assert_supported
from .generator import DocumentPlan

logger = logging.getLogger(__name__)

REWRITE_SYSTEM_PROMPT = """\
You rewrite one line of a job application document so it reads better.

Absolute rules:
  * Say ONLY what the source line says. Do not add skills, technologies, employers, job
    titles, dates, team sizes, percentages, or achievements that are not in it.
  * If the source line is already clear, return it unchanged.
  * Never invent a number. Never generalise "built a pipeline" into "led a team".
  * Reply with the rewritten line and nothing else -- no preamble, no quotes, no explanation.

You are improving phrasing, not writing a CV. Adding anything is a failure.
"""


def _prompt(text: str, job_title: str | None, company: str | None) -> str:
    context = ""
    if job_title:
        context = f"\nThis is for an application for {job_title}"
        context += f" at {company}." if company else "."
    return (
        f"Source line:\n{text}\n{context}\n\n"
        "Rewrite it in one or two sentences, adding nothing."
    )


async def polish(
    plan: DocumentPlan,
    router,
    *,
    supporting_values: list[str],
    max_items: int = 6,
) -> tuple[DocumentPlan, list[str]]:
    """Rewrite a plan's item texts in place, keeping every fact reference intact.

    Returns the plan and a list of notes describing anything that was rejected, so the
    result is honest about where the model was overruled rather than silently discarding it.

    `supporting_values` is the raw text of the accepted facts behind the document; a
    rewritten line may not name anything they do not.
    """
    notes: list[str] = []
    rewritten = 0

    for section in plan.sections:
        # The contact block backs the header and is never rendered as prose.
        if section.heading == "Contact":
            continue

        for item in section.items:
            if rewritten >= max_items:
                break
            # Short entries (a skill name) have nothing to improve and everything to lose.
            if len(item.text) < 40:
                continue

            try:
                candidate = await router.generate(
                    _prompt(item.text, plan.job_title, plan.company),
                    system=REWRITE_SYSTEM_PROMPT,
                )
            except (ProviderUnavailable, Exception) as exc:  # noqa: BLE001
                notes.append(f"model unavailable ({exc.__class__.__name__}); kept original text")
                return plan, notes

            candidate = (candidate or "").strip().strip('"').strip()
            if not candidate:
                continue

            try:
                # The one thing a rewriter can still get wrong.
                assert_supported(
                    candidate,
                    supporting_values + [item.text],
                    where=f"rewritten line in {section.heading!r}",
                )
            except UnsupportedClaim as exc:
                notes.append(str(exc) + " -- kept the original wording")
                logger.warning("rejected model rewrite: %s", exc)
                continue

            item.text = candidate
            rewritten += 1

    if rewritten:
        notes.insert(0, f"{rewritten} line(s) rewritten by the model.")
    return plan, notes
