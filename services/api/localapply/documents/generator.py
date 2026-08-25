"""Build grounded documents from accepted facts.

The rule this module exists to enforce: **every line of a generated document traces back to
an accepted fact.** Not "usually", not "the model was told not to exaggerate" -- structurally.
A document is assembled as a `DocumentPlan` in which each item carries the ids of the facts
backing it, and `assert_grounded` refuses to render a plan containing an item that cites
nothing, or that cites a fact you have not accepted.

That check sits between generation and rendering, so a model-backed generator (Phase 4)
passes through exactly the same gate. A model may phrase your experience; it may not invent
one.

The master CV is never modified automatically. Tailoring *selects and orders* facts; it does
not rewrite them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from ..profile.facts import FactCategory, FactStatus
from . import prose
from .matching import MatchResult, extract_requirements, match, order_skills, rank_experience
from .retrieval import select_bullets

# One page is the brief. A recruiter spends seconds on a CV, and an ATS does not reward
# length -- so the document is budgeted rather than allowed to grow with the profile. These
# are the caps that keep a rich profile (six roles, thirty skills, eleven projects) to a
# single readable page.
MAX_ROLES = 4
MAX_BULLETS_PER_ROLE = 2
MAX_EDUCATION = 3
MAX_PROJECTS = 2
MAX_CERTIFICATIONS = 3
#: A summary naming eight technologies is a keyword list with a full stop on the end.
MAX_SUMMARY_SKILLS = 4

# A cover letter is read in under a minute and is a *letter*, not a second CV. Two roles
# with two bullets each is as much evidence as one page of prose can carry before a reader
# starts skimming -- which is the point at which it stops being read at all.
MAX_LETTER_ROLES = 2
MAX_LETTER_BULLETS = 2
MAX_LETTER_SKILLS = 5


def _trim_detail(fact) -> dict:
    """Carry a fact's structure through, with its bullet list cut to the page budget."""
    detail = dict(getattr(fact, "detail", None) or {})
    bullets = detail.get("bullets") or []
    if bullets:
        detail["bullets"] = bullets[:MAX_BULLETS_PER_ROLE]
    return detail


class UngroundedDocument(RuntimeError):
    """A document tried to claim something no accepted fact supports."""


@dataclass
class DocumentItem:
    text: str
    #: Facts backing this line. An empty list is a bug, and `assert_grounded` says so.
    fact_ids: list[UUID] = field(default_factory=list)
    #: Structured parts carried through from the fact, so the template can lay out a role,
    #: its employer, its dates and its bullets instead of printing one joined string.
    detail: dict = field(default_factory=dict)


@dataclass
class DocumentSection:
    heading: str
    items: list[DocumentItem] = field(default_factory=list)
    #: Rendered as a comma-joined run rather than a bullet list.
    inline: bool = False

    @property
    def non_empty(self) -> bool:
        return bool(self.items)


@dataclass
class DocumentPlan:
    kind: str  # master_cv | tailored_cv | cover_letter
    title: str
    contact: dict[str, str] = field(default_factory=dict)
    sections: list[DocumentSection] = field(default_factory=list)
    job_title: str | None = None
    company: str | None = None
    match: MatchResult | None = None
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def fact_ids(self) -> set[UUID]:
        return {fid for section in self.sections for item in section.items
                for fid in item.fact_ids}

    def visible_sections(self) -> list[DocumentSection]:
        return [s for s in self.sections if s.non_empty]


def assert_grounded(plan: DocumentPlan, accepted_ids: set[UUID]) -> None:
    """Refuse a plan that claims anything the profile cannot back.

    Two failure modes, both fatal:
      * an item citing no fact at all -- generated prose with nothing behind it;
      * an item citing a fact that is not accepted -- a proposal, a rejected value, or a
        superseded one leaking into a document.
    """
    for section in plan.sections:
        for item in section.items:
            if not item.fact_ids:
                raise UngroundedDocument(
                    f"{plan.kind}: {section.heading!r} contains an unsupported line: "
                    f"{item.text[:80]!r}"
                )
            unknown = set(item.fact_ids) - accepted_ids
            if unknown:
                raise UngroundedDocument(
                    f"{plan.kind}: {section.heading!r} cites facts that are not accepted "
                    f"({', '.join(str(u) for u in sorted(unknown, key=str))})"
                )


# --------------------------------------------------------------------------------------


def _accepted(facts: list) -> list:
    return [f for f in facts if f.status == FactStatus.ACCEPTED.value]


def _by_category(facts: list, category: str) -> list:
    return [f for f in facts if f.category == category]


def _first(facts: list, key: str):
    return next((f for f in facts if f.key == key), None)


def _contact(facts: list) -> tuple[dict[str, str], list[UUID]]:
    contact: dict[str, str] = {}
    ids: list[UUID] = []
    for key in ("full_name", "email", "phone", "city", "country",
                "linkedin_url", "github_url", "portfolio_url", "current_title"):
        fact = _first(facts, key)
        if fact is not None:
            contact[key] = fact.value
            ids.append(fact.id)
    return contact, ids


class DocumentGenerator:
    """Deterministic assembly from accepted facts. No model, no invention."""

    name = "rules"

    def master_cv(self, facts: list) -> DocumentPlan:
        """Everything you have accepted, in a conventional order.

        This is the canonical record. Tailored documents are derived from it; it is never
        rewritten as a side effect of generating one.
        """
        facts = _accepted(facts)
        contact, contact_ids = _contact(facts)

        plan = DocumentPlan(
            kind="master_cv",
            title=contact.get("full_name", "Curriculum Vitae"),
            contact=contact,
        )
        if contact_ids:
            # The contact block is itself grounded, via a hidden section.
            plan.sections.append(
                DocumentSection("Contact", [DocumentItem("header", contact_ids)])
            )

        skills = _by_category(facts, FactCategory.SKILL.value)
        if skills:
            plan.sections.append(
                DocumentSection(
                    "Skills",
                    [DocumentItem(f.value, [f.id]) for f in skills],
                    inline=True,
                )
            )

        for heading, category, limit in (
            ("Experience", FactCategory.EXPERIENCE.value, MAX_ROLES),
            ("Education", FactCategory.EDUCATION.value, MAX_EDUCATION),
            ("Projects", FactCategory.PROJECT.value, MAX_PROJECTS),
            ("Certifications", FactCategory.CERTIFICATION.value, MAX_CERTIFICATIONS),
        ):
            entries = _by_category(facts, category)[:limit]
            if entries:
                plan.sections.append(
                    DocumentSection(
                        heading,
                        [
                            DocumentItem(f.value, [f.id], _trim_detail(f))
                            for f in entries
                        ],
                    )
                )
        return plan

    def tailored_cv(
        self, facts: list, *, job_title: str, company: str | None, description: str
    ) -> DocumentPlan:
        """The master CV, reordered for one job.

        Selection and ordering only. No fact's wording changes, and nothing is added that
        the master CV does not already contain -- so a tailored CV can never claim more than
        the canonical one.

        Three separate decisions, each made where it belongs: relevance chooses *which*
        roles and bullets appear, chronology decides the order they appear in, and the page
        budget decides how many. Conflating the first two is what put a job that ended in
        2023 above the one still held.
        """
        facts = _accepted(facts)
        skills = _by_category(facts, FactCategory.SKILL.value)
        skill_names = [f.value for f in skills]

        requirements = extract_requirements(description)
        result = match(requirements, set(skill_names), description)

        plan = self.master_cv(facts)
        plan.kind = "tailored_cv"
        plan.job_title = job_title
        plan.company = company
        plan.match = result

        # Curate before ordering: a CV showing every skill communicates less than one
        # showing the twelve that matter, and "CI"/"CD"/"CI-CD" side by side looks careless.
        from .retrieval import curate_skills

        curated = curate_skills(skills, description)
        ordered_names = order_skills([f.value for f in curated], result.matched)
        by_name = {f.value.casefold(): f for f in curated}
        experiences = _by_category(facts, FactCategory.EXPERIENCE.value)

        featured = prose.chronological(
            rank_experience(experiences, result.matched)[:MAX_ROLES]
        )
        matched_lower = {s.casefold() for s in result.matched}
        matched_skills = [f.value for f in curated if f.value.casefold() in matched_lower]

        for section in plan.sections:
            if section.heading == "Skills":
                section.items = [
                    DocumentItem(name, [by_name[name.casefold()].id])
                    for name in ordered_names
                    if name.casefold() in by_name
                ]
            elif section.heading == "Experience":
                section.items = [
                    DocumentItem(
                        f.value,
                        [f.id],
                        # Which bullets, not the first two the CV happened to list.
                        {**(getattr(f, "detail", None) or {}),
                         "bullets": select_bullets(f, description, MAX_BULLETS_PER_ROLE)},
                    )
                    for f in featured
                ]

        # The summary. A CV generated without a model had none at all, and it is the first
        # thing a recruiter reads. `write_tailored_cv` replaces this one when a model is
        # available; this is the floor, not a placeholder.
        summary = prose.profile_summary(
            plan.contact.get("current_title", ""),
            matched_skills[:MAX_SUMMARY_SKILLS],
            featured[0] if featured else None,
            job_title,
        )
        summary_ids = [f.id for f in curated if f.value in matched_skills[:MAX_SUMMARY_SKILLS]]
        summary_ids += [f.id for f in featured[:1]]
        title_fact = _first(facts, "current_title")
        if title_fact is not None:
            summary_ids.append(title_fact.id)
        if summary and summary_ids:
            insert_at = 1 if plan.sections and plan.sections[0].heading == "Contact" else 0
            plan.sections.insert(
                insert_at, DocumentSection("Summary", [DocumentItem(summary, summary_ids)])
            )

        return plan

    def cover_letter(
        self,
        facts: list,
        *,
        job_title: str,
        company: str | None,
        description: str,
    ) -> DocumentPlan:
        """A letter assembled from accepted facts.

        Every paragraph is *composed* from the structured parts of a fact rather than
        printed from it -- see documents/prose.py for why that distinction is the whole
        difference between a letter and a list of rows. It cannot claim a skill or a role
        you have not accepted, because only facts fill the sentence forms.

        It reads plainly rather than eloquently: that is the honest trade for not being
        able to invent. `writer.write_cover_letter` hands these paragraphs to a model to
        rewrite, with a claim check on every line that comes back.
        """
        facts = _accepted(facts)
        contact, contact_ids = _contact(facts)
        skills = _by_category(facts, FactCategory.SKILL.value)
        experiences = _by_category(facts, FactCategory.EXPERIENCE.value)

        result = match(extract_requirements(description), {f.value for f in skills},
                       description)
        matched_lower = {s.casefold() for s in result.matched}
        matched_facts = [f for f in skills if f.value.casefold() in matched_lower]

        plan = DocumentPlan(
            kind="cover_letter",
            title=f"Application for {job_title}",
            contact=contact,
            job_title=job_title,
            company=company,
            match=result,
        )

        body = DocumentSection("Letter")

        name_fact = _first(facts, "full_name")
        title_fact = _first(facts, "current_title")
        opening_ids = [f.id for f in (name_fact, title_fact) if f is not None]
        opening_ids += [f.id for f in matched_facts[:MAX_LETTER_SKILLS]]
        # An opening sentence is mostly the job title *you* typed, so it needs something of
        # yours behind it to be a grounded line at all. Falling back to the contact facts
        # covers a profile with an email but no name, title or matching skill; a profile
        # with nothing gets no opening, and the empty-letter refusal below catches it.
        opening_ids = opening_ids or contact_ids
        if opening_ids:
            body.items.append(
                DocumentItem(
                    prose.opening_paragraph(
                        job_title,
                        company,
                        title_fact.value if title_fact else "",
                        [f.value for f in matched_facts[:MAX_LETTER_SKILLS]],
                    ),
                    opening_ids,
                )
            )

        # The evidence. One paragraph per role, written as prose, with the bullets that
        # answer this posting -- not the first two the CV happened to list.
        #
        # Relevance picks *which* roles; a current one is then written first. Leading with
        # a job that ended two years ago and following it with the one still held reads as
        # a chronology error even when the ranking behind it is right.
        featured = prose.chronological(
            rank_experience(experiences, result.matched)[:MAX_LETTER_ROLES]
        )
        for entry in featured:
            chosen = select_bullets(entry, description, limit=MAX_LETTER_BULLETS)
            body.items.append(
                DocumentItem(
                    prose.experience_paragraph(entry, chosen),
                    [entry.id],
                    getattr(entry, "detail", {}) or {},
                )
            )

        # Fit last, so the letter leads with what was done rather than with a keyword list.
        fit = prose.fit_paragraph(
            [f.value for f in matched_facts[:MAX_LETTER_SKILLS]],
            result.missing_required[:2],
        )
        if fit and matched_facts:
            body.items.append(
                DocumentItem(fit, [f.id for f in matched_facts[:MAX_LETTER_SKILLS]])
            )

        if contact_ids:
            body.items.append(
                DocumentItem(
                    prose.closing_paragraph(
                        contact.get("email", ""), contact.get("phone", "")
                    ),
                    contact_ids,
                )
            )

        body.items = [item for item in body.items if item.text.strip()]
        plan.sections.append(body)

        # A letter with nothing behind it is worse than no letter.
        if not body.items:
            raise UngroundedDocument(
                "No accepted facts to write a cover letter from. Accept some profile facts "
                "first."
            )
        return plan


class LLMDocumentGenerator(DocumentGenerator):
    """Phase 4. Same interface, and the same grounding gate.

    A model may rephrase an accepted fact into better prose. It may not add a section, a
    skill, or a role -- `assert_grounded` rejects any item whose fact_ids do not resolve.
    """

    name = "llm"

    def __init__(self, router) -> None:
        self._router = router

    def cover_letter(self, facts, **kwargs) -> DocumentPlan:  # pragma: no cover
        raise NotImplementedError("Model-backed writing lands with the AI engine.")
