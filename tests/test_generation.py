"""Document generation: matching, grounding, and rendering.

The central claim being tested is that a generated document cannot say anything your
accepted facts do not support -- including when a fact is only *proposed*, or was rejected,
or has since been superseded.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from localapply.documents.generator import (
    DocumentGenerator,
    DocumentItem,
    DocumentPlan,
    DocumentSection,
    UngroundedDocument,
    assert_grounded,
)
from localapply.documents.matching import (
    extract_requirements,
    match,
    order_skills,
    rank_experience,
    years_requested,
)
from localapply.documents.render import render_html
from localapply.profile.facts import FactCategory, FactStatus

JOB_DESCRIPTION = """
We are looking for an AI Engineer to build retrieval-augmented systems.

Requirements:
- Strong Python, with production FastAPI experience
- Hands-on RAG: chunking, embeddings, evaluation
- Docker and comfort operating services you have built
- 3+ years of professional experience

Nice to have:
- Kubernetes
- Rust
"""


class Fact:
    """Stands in for db.models.ProfileFact without a database."""

    def __init__(self, key, value, category, status=FactStatus.ACCEPTED.value):
        self.id = uuid4()
        self.key = key
        self.value = value
        self.category = category
        self.status = status


@pytest.fixture
def facts():
    return [
        Fact("full_name", "Haidar Farhat", FactCategory.IDENTITY.value),
        Fact("email", "haidar@example.com", FactCategory.IDENTITY.value),
        Fact("phone", "+961 71 234 567", FactCategory.IDENTITY.value),
        Fact("current_title", "Senior AI Engineer", FactCategory.IDENTITY.value),
        Fact("Python", "Python", FactCategory.SKILL.value),
        Fact("FastAPI", "FastAPI", FactCategory.SKILL.value),
        Fact("RAG", "RAG", FactCategory.SKILL.value),
        Fact("Docker", "Docker", FactCategory.SKILL.value),
        Fact("Laravel", "Laravel", FactCategory.SKILL.value),
        Fact("Fitly", "Senior AI Engineer, Fitly - built a RAG pipeline with FastAPI",
             FactCategory.EXPERIENCE.value),
        Fact("CarePool", "Backend Engineer, CarePool - Laravel scheduling platform",
             FactCategory.EXPERIENCE.value),
        Fact("BSc", "BSc Computer Science, Lebanese University",
             FactCategory.EDUCATION.value),
    ]


@pytest.fixture
def accepted_ids(facts):
    return {f.id for f in facts}


@pytest.fixture
def generator():
    return DocumentGenerator()


# --------------------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------------------


def test_requirements_split_into_required_and_optional():
    requirements = {r.skill: r for r in extract_requirements(JOB_DESCRIPTION)}

    assert requirements["Python"].required is True
    assert requirements["FastAPI"].required is True
    assert requirements["Kubernetes"].required is False
    assert requirements["Rust"].required is False


def test_years_requested_is_read():
    assert years_requested(JOB_DESCRIPTION) == 3
    assert years_requested("no numbers here") is None


def test_match_reports_what_is_missing(facts):
    skills = {f.value for f in facts if f.category == FactCategory.SKILL.value}
    result = match(extract_requirements(JOB_DESCRIPTION), skills, JOB_DESCRIPTION)

    assert set(result.matched_required) >= {"Python", "FastAPI", "RAG", "Docker"}
    assert "Kubernetes" in result.missing_optional
    assert result.score > 0.8
    assert result.recommendation == "apply"


def test_a_missing_required_skill_lowers_the_score(facts):
    result = match(extract_requirements(JOB_DESCRIPTION), {"Python"}, JOB_DESCRIPTION)
    assert "FastAPI" in result.missing_required
    assert result.score < 0.55
    assert result.recommendation == "skip"


def test_nothing_recognised_scores_zero_not_a_flattering_hundred():
    result = match(extract_requirements("We want a friendly person."), {"Python"})
    assert result.score == 0.0


def test_experience_is_ranked_by_relevance(facts):
    experiences = [f for f in facts if f.category == FactCategory.EXPERIENCE.value]
    ranked = rank_experience(experiences, ["RAG", "FastAPI"])
    assert "Fitly" in ranked[0].value


def test_skill_ordering_puts_requested_skills_first():
    ordered = order_skills(["Laravel", "Python", "Docker"], ["Docker", "Python"])
    assert ordered[:2] == ["Python", "Docker"]
    assert ordered[-1] == "Laravel"


# --------------------------------------------------------------------------------------
# Grounding -- the property that matters
# --------------------------------------------------------------------------------------


def test_master_cv_is_fully_grounded(facts, accepted_ids, generator):
    plan = generator.master_cv(facts)
    assert_grounded(plan, accepted_ids)  # must not raise
    assert plan.fact_ids <= accepted_ids


def test_tailored_cv_is_fully_grounded(facts, accepted_ids, generator):
    plan = generator.tailored_cv(
        facts, job_title="AI Engineer", company="Northwind", description=JOB_DESCRIPTION
    )
    assert_grounded(plan, accepted_ids)


def test_cover_letter_is_fully_grounded(facts, accepted_ids, generator):
    plan = generator.cover_letter(
        facts, job_title="AI Engineer", company="Northwind", description=JOB_DESCRIPTION
    )
    assert_grounded(plan, accepted_ids)


def test_an_item_citing_nothing_is_refused():
    """Generated prose with no fact behind it is exactly what this gate exists to stop."""
    plan = DocumentPlan(kind="cover_letter", title="x")
    plan.sections.append(
        DocumentSection("Letter", [DocumentItem("I led a team of 40 engineers.", [])])
    )
    with pytest.raises(UngroundedDocument, match="unsupported line"):
        assert_grounded(plan, set())


def test_an_item_citing_an_unaccepted_fact_is_refused():
    stranger = uuid4()
    plan = DocumentPlan(kind="tailored_cv", title="x")
    plan.sections.append(DocumentSection("Skills", [DocumentItem("Kubernetes", [stranger])]))
    with pytest.raises(UngroundedDocument, match="not accepted"):
        assert_grounded(plan, {uuid4()})


def test_a_proposed_fact_cannot_reach_a_document(facts, generator):
    """A CV import proposal must not appear in a generated CV before you accept it."""
    facts.append(Fact("Kubernetes", "Kubernetes", FactCategory.SKILL.value,
                      status=FactStatus.PROPOSED.value))
    accepted = {f.id for f in facts if f.status == FactStatus.ACCEPTED.value}

    plan = generator.master_cv(facts)

    assert_grounded(plan, accepted)
    rendered = render_html(plan)
    assert "Kubernetes" not in rendered


def test_a_rejected_fact_cannot_reach_a_document(facts, generator):
    facts.append(Fact("Rust", "Rust", FactCategory.SKILL.value,
                      status=FactStatus.REJECTED.value))
    accepted = {f.id for f in facts if f.status == FactStatus.ACCEPTED.value}

    plan = generator.master_cv(facts)
    assert_grounded(plan, accepted)
    assert "Rust" not in render_html(plan)


def test_a_superseded_fact_cannot_reach_a_document(facts, generator):
    facts.append(Fact("email", "old@example.com", FactCategory.IDENTITY.value,
                      status=FactStatus.SUPERSEDED.value))
    plan = generator.master_cv(facts)
    assert "old@example.com" not in render_html(plan)


def test_a_missing_skill_is_never_claimed(facts, generator):
    """The job asks for Kubernetes; the profile has none. It must not appear anywhere."""
    plan = generator.tailored_cv(
        facts, job_title="AI Engineer", company="Northwind", description=JOB_DESCRIPTION
    )
    rendered = render_html(plan)
    assert "Kubernetes" not in rendered
    assert "Kubernetes" in plan.match.missing_optional


def test_cover_letter_without_facts_refuses_rather_than_padding(generator):
    with pytest.raises(UngroundedDocument, match="No accepted facts"):
        generator.cover_letter([], job_title="AI Engineer", company="X", description="")


# --------------------------------------------------------------------------------------
# Tailoring behaviour
# --------------------------------------------------------------------------------------


def test_tailoring_reorders_but_never_adds(facts, generator):
    master = generator.master_cv(facts)
    tailored = generator.tailored_cv(
        facts, job_title="AI Engineer", company="Northwind", description=JOB_DESCRIPTION
    )
    # A tailored CV can never claim more than the canonical one.
    assert tailored.fact_ids <= master.fact_ids


def test_tailoring_puts_requested_skills_first(facts, generator):
    tailored = generator.tailored_cv(
        facts, job_title="AI Engineer", company="Northwind", description=JOB_DESCRIPTION
    )
    skills = next(s for s in tailored.sections if s.heading == "Skills")
    names = [item.text for item in skills.items]

    assert names.index("Python") < names.index("Laravel")
    assert names.index("RAG") < names.index("Laravel")


def test_generating_a_tailored_cv_does_not_mutate_the_master(facts, generator):
    before = [item.text for item in
              next(s for s in generator.master_cv(facts).sections if s.heading == "Skills").items]
    generator.tailored_cv(
        facts, job_title="AI Engineer", company="X", description=JOB_DESCRIPTION
    )
    after = [item.text for item in
             next(s for s in generator.master_cv(facts).sections if s.heading == "Skills").items]

    assert before == after


# --------------------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------------------


def test_rendered_cv_contains_the_facts(facts, generator):
    rendered = render_html(generator.master_cv(facts))
    for expected in ("Haidar Farhat", "haidar@example.com", "Python", "Lebanese University"):
        assert expected in rendered


def test_rendered_tailored_cv_names_the_job(facts, generator):
    plan = generator.tailored_cv(
        facts, job_title="AI Engineer", company="Northwind", description=JOB_DESCRIPTION
    )
    rendered = render_html(plan)
    assert "AI Engineer" in rendered
    assert "Northwind" in rendered


def test_rendered_cover_letter_reads_as_a_letter(facts, generator):
    plan = generator.cover_letter(
        facts, job_title="AI Engineer", company="Northwind", description=JOB_DESCRIPTION
    )
    rendered = render_html(plan)
    assert "Dear Hiring Team" in rendered
    assert "Kind regards" in rendered
    assert "AI Engineer" in rendered


def test_rendering_escapes_hostile_fact_values(generator):
    """Fact values can come from an uploaded document, so they are not trusted markup."""
    facts = [
        Fact("full_name", "Haidar Farhat", FactCategory.IDENTITY.value),
        Fact("x", "<script>alert(1)</script>", FactCategory.SKILL.value),
    ]
    rendered = render_html(generator.master_cv(facts))
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered


def test_contact_block_is_not_rendered_as_a_section(facts, generator):
    """It backs the header; showing a 'Contact' heading with the word 'header' under it
    would be the grounding mechanism leaking into the output."""
    rendered = render_html(generator.master_cv(facts))
    assert "<h2>Contact</h2>" not in rendered
    assert ">header<" not in rendered
