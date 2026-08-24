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
from .matching import MatchResult, extract_requirements, match, order_skills, rank_experience

# One page is the brief. A recruiter spends seconds on a CV, and an ATS does not reward
# length -- so the document is budgeted rather than allowed to grow with the profile. These
# are the caps that keep a rich profile (six roles, thirty skills, eleven projects) to a
# single readable page.
MAX_ROLES = 4
MAX_BULLETS_PER_ROLE = 2
MAX_EDUCATION = 3
MAX_PROJECTS = 2
MAX_CERTIFICATIONS = 3


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

        for section in plan.sections:
            if section.heading == "Skills":
                section.items = [
                    DocumentItem(name, [by_name[name.casefold()].id])
                    for name in ordered_names
                    if name.casefold() in by_name
                ]
            elif section.heading == "Experience":
                section.items = [
                    DocumentItem(f.value, [f.id], getattr(f, "detail", {}) or {})
                    for f in rank_experience(experiences, result.matched)
                ]

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

        Composed from sentence forms filled with facts, so it cannot claim a skill or a role
        you have not accepted. It reads plainly rather than eloquently -- that is the honest
        trade for not being able to invent. A model-backed writer lands in Phase 4 and will
        pass through `assert_grounded` unchanged.
        """
        facts = _accepted(facts)
        contact, contact_ids = _contact(facts)
        skills = _by_category(facts, FactCategory.SKILL.value)
        experiences = _by_category(facts, FactCategory.EXPERIENCE.value)

        result = match(extract_requirements(description), {f.value for f in skills},
                       description)
        matched_lower = {s.casefold() for s in result.matched}
        matched_facts = [f for f in skills if f.value.casefold() in matched_lower]

        where = f" at {company}" if company else ""
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
        if opening_ids:
            role = f", currently working as {title_fact.value}" if title_fact else ""
            body.items.append(
                DocumentItem(
                    f"I am writing to apply for the {job_title} position{where}. "
                    f"I am {name_fact.value}{role}." if name_fact
                    else f"I am writing to apply for the {job_title} position{where}.",
                    opening_ids,
                )
            )

        if matched_facts:
            listed = ", ".join(f.value for f in matched_facts[:8])
            body.items.append(
                DocumentItem(
                    f"The role asks for {listed}, which are among the skills I work with.",
                    [f.id for f in matched_facts[:8]],
                )
            )

        for entry in rank_experience(experiences, result.matched)[:2]:
            body.items.append(
                DocumentItem(entry.value, [entry.id], getattr(entry, "detail", {}) or {})
            )

        if contact_ids:
            body.items.append(
                DocumentItem(
                    "I would welcome the chance to discuss the role. "
                    + (f"You can reach me at {contact['email']}."
                       if "email" in contact else ""),
                    contact_ids,
                )
            )

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
