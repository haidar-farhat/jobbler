"""Agentic document writing: retrieve, draft, critique, revise.

The rule-based generator is truthful and dull. It prints facts. This module keeps the
truthfulness and fixes the dullness, by giving the model real work to do inside a boundary
it cannot cross.

The loop, per document:

    1. RETRIEVE  score every fact against the posting; select the entries and the bullets
                 that answer it (documents/retrieval.py)
    2. DRAFT     the model writes a professional summary and rewrites the selected bullets
                 as achievements -- from the fact text, and nothing else
    3. CRITIQUE  the model reads its own draft against the posting and the source facts and
                 says what is vague, unsupported, or missing
    4. REVISE    one more pass addressing the critique
    5. VERIFY    every produced line is claim-checked; anything unsupported is thrown away
                 and the deterministic wording used instead

Why this is safe even though the model now writes real prose: it is never given a blank
page. Each call receives one fact's text and must restate that. `claims.check_claims`
compares the result against the source and rejects any technology or figure that was not in
it, and `assert_grounded` still runs over the finished plan. A model that embellishes loses
its embellishment; it cannot get a claim through.

What this does NOT do, deliberately: infer, combine, or conclude. "Built a RAG pipeline" is
not allowed to become "led the company's AI strategy", because there is no way to tell that
apart from a lie from outside the sentence.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from ..profile.facts import FactCategory
from .claims import check_claims
from .generator import DocumentItem, DocumentPlan, DocumentSection
from .matching import MatchResult
from .retrieval import Relevance, score_facts, select_bullets

logger = logging.getLogger(__name__)

MAX_FEATURED_ROLES = 4
SUMMARY_MAX_WORDS = 60


SUMMARY_SYSTEM = """\
You write the opening summary of a CV: two or three sentences, first person implied, no
padding.

You are given the candidate's verified facts. Rules, in order of importance:
  * Use ONLY those facts. No technology, employer, figure, or claim that is not in them.
  * No invented seniority, team sizes, or years. If a number is not in the facts, do not
    write a number.
  * No filler. Ban: "passionate", "results-driven", "team player", "proven track record",
    "dynamic", "synergy", "leverage", "wheelhouse".
  * Concrete over grand. Name the actual work.

Reply with the summary text only. No heading, no quotes, no preamble.
"""

BULLET_SYSTEM = """\
You rewrite one CV bullet point so it reads well.

Rules:
  * Say ONLY what the source bullet says. Add no technology, employer, metric, or outcome
    that is not already in it.
  * Keep every number exactly as written. Never introduce one.
  * Start with a strong verb. Cut "responsible for", "worked on", "helped with".
  * One sentence. Under 28 words.

Reply with the rewritten bullet only.
"""

CRITIQUE_SYSTEM = """\
You review a draft CV section against the job posting and the candidate's source facts.

Say briefly what is weak: vague wording, buried relevance, anything that reads as filler.
If a line states something the source facts do not support, say so first and quote it.

Be specific and short: at most four bullet points. If it is genuinely good, say "good".
"""


@dataclass
class WriteReport:
    """What the writer did, and where it was overruled. Surfaced to the user."""

    rewritten: int = 0
    rejected: list[str] = field(default_factory=list)
    critique: str = ""
    summary_written: bool = False
    featured: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "rewritten": self.rewritten,
            "rejected": self.rejected,
            "critique": self.critique,
            "summary_written": self.summary_written,
            "featured": self.featured,
            "notes": self.notes,
        }


def _supported(candidate: str, source: str, report: WriteReport, where: str) -> str | None:
    """Return the candidate only if it claims nothing the source does not."""
    text = (candidate or "").strip().strip('"').strip()
    if not text:
        return None

    result = check_claims(text, [source])
    if not result.clean:
        report.rejected.append(f"{where}: {result.describe()}")
        logger.warning("rejected model output in %s: %s", where, result.describe())
        return None

    # A "rewrite" several times the length of its source is padding, not phrasing.
    if len(text) > max(240, len(source) * 2):
        report.rejected.append(f"{where}: rewrite was much longer than the source")
        return None
    return text


def _facts_block(relevances: list[Relevance], limit: int = 14) -> str:
    lines = []
    for relevance in relevances[:limit]:
        fact = relevance.fact
        detail = getattr(fact, "detail", None) or {}
        headline = detail.get("role") or fact.value
        organisation = detail.get("organisation")
        dates = detail.get("dates")
        parts = [p for p in (headline, organisation, dates) if p]
        lines.append("  - " + " | ".join(parts))
        for bullet in (detail.get("bullets") or [])[:2]:
            lines.append(f"      {bullet}")
    return "\n".join(lines) or "  (none)"


class AgenticWriter:
    """Retrieve -> draft -> critique -> revise, with a claim check on every produced line."""

    name = "agentic"

    def __init__(self, router) -> None:
        self._router = router

    async def _generate(self, prompt: str, system: str) -> str:
        try:
            return await self._router.generate(prompt, system=system)
        except Exception as exc:  # noqa: BLE001 - a dead model degrades, never breaks
            logger.warning("model unavailable during writing: %s", exc)
            return ""

    # -- 1. retrieve -------------------------------------------------------------------

    def plan_content(
        self, facts: list, description: str
    ) -> tuple[list[Relevance], list[Relevance]]:
        """Choose which experience to feature, and in what order."""
        experiences = [f for f in facts if f.category == FactCategory.EXPERIENCE.value]
        ranked = score_facts(experiences, description)
        featured = [r for r in ranked if r.score > 0][:MAX_FEATURED_ROLES]
        # Never show an empty Experience section just because nothing matched keywords.
        if not featured:
            featured = ranked[:MAX_FEATURED_ROLES]
        return featured, ranked

    # -- 2. draft ----------------------------------------------------------------------

    async def write_summary(
        self,
        facts: list,
        featured: list[Relevance],
        match: MatchResult | None,
        job_title: str | None,
        report: WriteReport,
    ) -> DocumentItem | None:
        """The opening paragraph, which the rule-based CV does not have at all."""
        title_fact = next((f for f in facts if f.key == "current_title"), None)
        skills = [f for f in facts if f.category == FactCategory.SKILL.value]
        if not featured and not skills:
            return None

        relevant_skills = [s.value for s in skills][:10]
        if match:
            wanted = {s.casefold() for s in match.matched}
            ordered = [s for s in relevant_skills if s.casefold() in wanted]
            relevant_skills = ordered + [s for s in relevant_skills if s not in ordered]

        source = "\n".join(
            [
                f"Current title: {title_fact.value}" if title_fact else "",
                "Skills: " + ", ".join(relevant_skills),
                "Experience:",
                _facts_block(featured),
            ]
        ).strip()

        prompt = (
            (f"The candidate is applying for: {job_title}\n\n" if job_title else "")
            + f"Verified facts:\n{source}\n\n"
            + f"Write the CV summary in at most {SUMMARY_MAX_WORDS} words."
        )

        text = _supported(
            await self._generate(prompt, SUMMARY_SYSTEM), source, report, "summary"
        )
        if not text:
            return None

        # Trim to the word budget rather than letting a chatty model dominate the page.
        words = text.split()
        if len(words) > SUMMARY_MAX_WORDS + 15:
            text = " ".join(words[: SUMMARY_MAX_WORDS]) + "…"

        report.summary_written = True
        contributing = [f.id for f in facts if f.key == "current_title"]
        contributing += [r.fact.id for r in featured]
        contributing += [s.id for s in skills[:10]]
        return DocumentItem(text=text, fact_ids=contributing)

    async def write_bullets(
        self, relevance: Relevance, description: str, report: WriteReport
    ) -> list[str]:
        """Rewrite an entry's selected bullets, keeping the original when overruled."""
        chosen = select_bullets(relevance.fact, description)
        written: list[str] = []

        for bullet in chosen:
            candidate = await self._generate(
                f"Source bullet:\n{bullet}\n\nRewrite it.", BULLET_SYSTEM
            )
            text = _supported(candidate, bullet, report, f"bullet in {relevance.fact.key[:40]}")
            if text:
                report.rewritten += 1
                written.append(text)
            else:
                written.append(bullet)
        return written

    # -- 3. critique and 4. revise ------------------------------------------------------

    async def critique(
        self, plan: DocumentPlan, description: str, source_facts: list[str]
    ) -> str:
        """Have the model read its own draft. Advisory only -- it changes nothing by itself."""
        rendered = []
        for section in plan.visible_sections():
            if section.heading == "Contact":
                continue
            rendered.append(section.heading.upper())
            rendered += [f"  {item.text}" for item in section.items]

        prompt = (
            f"JOB POSTING:\n{description[:1500]}\n\n"
            f"SOURCE FACTS:\n" + "\n".join(f"  - {s}" for s in source_facts[:20]) + "\n\n"
            "DRAFT:\n" + "\n".join(rendered[:60])
        )
        return (await self._generate(prompt, CRITIQUE_SYSTEM)).strip()[:900]

    async def revise_summary(
        self, summary: DocumentItem, critique: str, source: str, report: WriteReport
    ) -> DocumentItem:
        """One revision pass. Kept to the summary: it is the only free prose in the document
        and therefore the only part a critique can meaningfully improve."""
        if not critique or critique.lower().startswith("good"):
            return summary

        candidate = await self._generate(
            f"Verified facts:\n{source}\n\nYour draft:\n{summary.text}\n\n"
            f"A reviewer said:\n{critique}\n\nRewrite the summary addressing that. "
            f"At most {SUMMARY_MAX_WORDS} words. Add no new facts.",
            SUMMARY_SYSTEM,
        )
        text = _supported(candidate, source, report, "revised summary")
        if text:
            report.notes.append("Summary revised after self-critique.")
            return DocumentItem(text=text, fact_ids=summary.fact_ids)
        return summary


_SPACE_RE = re.compile(r"\s+")


def tidy(text: str) -> str:
    return _SPACE_RE.sub(" ", text or "").strip()


async def write_tailored_cv(
    facts: list,
    *,
    job_title: str,
    company: str | None,
    description: str,
    router,
    base_plan: DocumentPlan,
    match: MatchResult | None = None,
) -> tuple[DocumentPlan, WriteReport]:
    """Run the full pipeline over a plan the rule-based generator already grounded.

    `base_plan` arrives correct-but-flat. Everything below improves its *wording* and its
    *selection*; the set of facts it may cite never grows, so `assert_grounded` on the way
    out still holds against exactly the accepted set.
    """
    writer = AgenticWriter(router)
    report = WriteReport()

    accepted = {f.id: f for f in facts}
    featured, ranked = writer.plan_content(facts, description)
    report.featured = [
        f"{(getattr(r.fact, 'detail', {}) or {}).get('role') or r.fact.key} ({r.why})"
        for r in featured
    ]

    # --- summary -------------------------------------------------------------------
    summary_item = await writer.write_summary(facts, featured, match, job_title, report)

    # --- experience: selected entries, rewritten bullets ----------------------------
    experience_items: list[DocumentItem] = []
    for relevance in featured:
        fact = relevance.fact
        detail = dict(getattr(fact, "detail", None) or {})
        bullets = await writer.write_bullets(relevance, description, report)
        if bullets:
            detail["bullets"] = bullets
        experience_items.append(
            DocumentItem(text=fact.value, fact_ids=[fact.id], detail=detail)
        )

    plan = base_plan
    plan.kind = "tailored_cv"
    plan.job_title = job_title
    plan.company = company
    if match is not None:
        plan.match = match

    if summary_item is not None:
        # Directly after the header, where a summary belongs.
        insert_at = 1 if plan.sections and plan.sections[0].heading == "Contact" else 0
        plan.sections.insert(insert_at, DocumentSection("Summary", [summary_item]))

    for section in plan.sections:
        if section.heading == "Experience" and experience_items:
            section.items = experience_items

    # --- critique and one revision --------------------------------------------------
    source_lines = [f.value for f in facts][:20]
    report.critique = await writer.critique(plan, description, source_lines)

    if summary_item is not None and report.critique:
        source = "\n".join(source_lines)
        revised = await writer.revise_summary(summary_item, report.critique, source, report)
        for section in plan.sections:
            if section.heading == "Summary":
                section.items = [revised]

    # --- final guard ----------------------------------------------------------------
    # Nothing above may have introduced a fact id that is not accepted. Cheap to prove.
    for section in plan.sections:
        for item in section.items:
            unknown = [fid for fid in item.fact_ids if fid not in accepted]
            if unknown:
                raise RuntimeError(
                    f"writer produced an item citing unaccepted facts in {section.heading!r}"
                )

    if report.rejected:
        report.notes.append(
            f"{len(report.rejected)} model rewrite(s) rejected for claiming something the "
            "facts do not support; the original wording was kept."
        )
    return plan, report
