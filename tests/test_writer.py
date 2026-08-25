"""Retrieval and the agentic writing pipeline.

Uses `ScriptedProvider`, so the cases that matter -- the model padding, inventing, or being
down -- are provoked deterministically rather than hoped for.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from localapply.ai.providers.scripted import ScriptedProvider
from localapply.ai.router import ModelRouter
from localapply.documents.generator import DocumentGenerator, assert_grounded
from localapply.documents.retrieval import (
    curate_skills,
    score_facts,
    select_bullets,
    skills_in,
)
from localapply.documents.writer import write_tailored_cv
from localapply.profile.facts import FactCategory, FactStatus

JOB = """Senior AI Engineer

Requirements:
- Strong Python and production FastAPI
- Hands-on RAG
- Docker

Nice to have:
- Kubernetes
"""


class Fact:
    def __init__(self, key, value, category, detail=None):
        self.id = uuid4()
        self.key, self.value, self.category = key, value, category
        self.detail = detail or {}
        self.status = FactStatus.ACCEPTED.value


@pytest.fixture
def facts():
    return [
        Fact("full_name", "Haidar Farhat", FactCategory.IDENTITY.value),
        Fact("email", "haidar@example.com", FactCategory.IDENTITY.value),
        Fact("current_title", "Senior AI Engineer", FactCategory.IDENTITY.value),
        Fact("Python", "Python", FactCategory.SKILL.value),
        Fact("FastAPI", "FastAPI", FactCategory.SKILL.value),
        Fact("RAG", "RAG", FactCategory.SKILL.value),
        Fact("Docker", "Docker", FactCategory.SKILL.value),
        Fact("Laravel", "Laravel", FactCategory.SKILL.value),
        Fact(
            "Senior AI Engineer — Fitly",
            "Senior AI Engineer — Fitly (2023 - Present)",
            FactCategory.EXPERIENCE.value,
            {
                "role": "Senior AI Engineer", "organisation": "Fitly",
                "dates": "2023 - Present",
                "bullets": [
                    "Built a RAG pipeline over 40k documents; cut retrieval latency by 60%.",
                    "Owned the evaluation harness and the FastAPI services in front of it.",
                    "Ran the weekly team retrospective.",
                ],
            },
        ),
        Fact(
            "Barista — Coffee House",
            "Barista — Coffee House (2019 - 2021)",
            FactCategory.EXPERIENCE.value,
            {"role": "Barista", "organisation": "Coffee House", "dates": "2019 - 2021",
             "bullets": ["Served customers."]},
        ),
    ]


@pytest.fixture
def provider():
    return ScriptedProvider()


@pytest.fixture
def router(provider):
    return ModelRouter(provider, vram_budget_mb=8151)


def base_plan(facts):
    return DocumentGenerator().tailored_cv(
        facts, job_title="Senior AI Engineer", company="Northwind", description=JOB
    )


async def write(facts, router):
    return await write_tailored_cv(
        facts, job_title="Senior AI Engineer", company="Northwind", description=JOB,
        router=router, base_plan=base_plan(facts), match=base_plan(facts).match,
    )


# --------------------------------------------------------------------------------------
# Retrieval
# --------------------------------------------------------------------------------------


def test_relevant_experience_outranks_unrelated(facts):
    experiences = [f for f in facts if f.category == FactCategory.EXPERIENCE.value]
    ranked = score_facts(experiences, JOB)

    assert "Fitly" in ranked[0].fact.value
    assert ranked[0].score > ranked[-1].score
    assert "RAG" in ranked[0].matched_skills


def test_relevance_explains_itself(facts):
    experiences = [f for f in facts if f.category == FactCategory.EXPERIENCE.value]
    top = score_facts(experiences, JOB)[0]
    assert top.why.startswith("answers")
    assert "FastAPI" in top.why or "RAG" in top.why


def test_bullets_are_chosen_by_relevance(facts):
    fitly = next(f for f in facts if "Fitly" in f.value)
    chosen = select_bullets(fitly, JOB, limit=2)

    assert len(chosen) == 2
    assert any("RAG pipeline" in b for b in chosen)
    # The retrospective bullet answers nothing in the posting.
    assert not any("retrospective" in b for b in chosen)


def test_skills_in_finds_whole_names_only():
    found = skills_in("We use Python and Docker, but not Pythonic style.")
    assert "Python" in found and "Docker" in found


def test_curation_drops_fragment_duplicates():
    """"CI", "CD" and "CI/CD" side by side looks careless; the specific name wins."""
    skills = [
        Fact("CI", "CI", FactCategory.SKILL.value),
        Fact("CD", "CD", FactCategory.SKILL.value),
        Fact("CI/CD", "CI/CD", FactCategory.SKILL.value),
        Fact("Python", "Python", FactCategory.SKILL.value),
    ]
    kept = {f.value for f in curate_skills(skills, JOB)}

    assert "CI/CD" in kept
    assert "CI" not in kept and "CD" not in kept
    assert "Python" in kept


def test_curation_puts_requested_skills_first(facts):
    skills = [f for f in facts if f.category == FactCategory.SKILL.value]
    ordered = [f.value for f in curate_skills(skills, JOB)]
    assert ordered.index("Python") < ordered.index("Laravel")


def test_curation_caps_the_number_shown():
    many = [Fact(f"S{i}", f"Skill{i}", FactCategory.SKILL.value) for i in range(40)]
    assert len(curate_skills(many, JOB, limit=12)) == 12


# --------------------------------------------------------------------------------------
# The pipeline
# --------------------------------------------------------------------------------------


async def test_a_summary_is_added(facts, router, provider):
    """The rule-based CV has no summary at all; this is the main quality gap it closes."""
    provider.replies = ["Senior AI Engineer who built a RAG pipeline using FastAPI."]
    plan, report = await write(facts, router)

    summary = next((s for s in plan.sections if s.heading == "Summary"), None)
    assert summary is not None
    assert report.summary_written
    assert "RAG" in summary.items[0].text


async def test_the_summary_is_grounded_in_real_facts(facts, router, provider):
    provider.replies = ["Senior AI Engineer who built a RAG pipeline using FastAPI."]
    plan, _ = await write(facts, router)
    assert_grounded(plan, {f.id for f in facts})


async def test_an_invented_summary_is_thrown_away(facts, router, provider):
    """The whole safety claim of this module, at its most likely failure point."""
    provider.replies = ["Kubernetes expert who led a team of 40 engineers across three offices."]
    plan, report = await write(facts, router)

    # The rejected text is gone. What remains is the summary the rule-based generator
    # composed from facts -- the floor a rejected rewrite falls back to, not a placeholder.
    summary = next((s for s in plan.sections if s.heading == "Summary"), None)
    assert summary is not None
    text = summary.items[0].text
    assert "Kubernetes" not in text, "an unsupported summary must not reach the document"
    assert "40 engineers" not in text
    assert report.rejected
    assert any("Kubernetes" in r for r in report.rejected)


async def test_an_invented_bullet_falls_back_to_the_original(facts, router, provider):
    provider.replies = ["Deployed Kubernetes clusters serving 5000 customers."]
    plan, report = await write(facts, router)

    experience = next(s for s in plan.sections if s.heading == "Experience")
    bullets = experience.items[0].detail["bullets"]

    assert any("RAG pipeline over 40k documents" in b for b in bullets)
    assert not any("Kubernetes" in b for b in bullets)
    assert report.rejected


async def test_padding_is_rejected(facts, router, provider):
    """A "rewrite" many times longer than its source is padding, not phrasing."""
    provider.replies = ["Built a RAG pipeline. " * 40]
    _, report = await write(facts, router)
    assert any("longer than the source" in r for r in report.rejected)


async def test_the_writer_never_widens_the_fact_set(facts, router, provider):
    provider.replies = ["Senior AI Engineer building RAG systems with FastAPI."]
    plan, _ = await write(facts, router)

    allowed = {f.id for f in facts}
    for section in plan.sections:
        for item in section.items:
            assert set(item.fact_ids) <= allowed


async def test_a_dead_model_still_produces_a_document(facts, router, provider):
    """Degrade to the deterministic CV rather than failing the generation."""
    provider.fail_with = ConnectionError("refused")
    plan, report = await write(facts, router)

    assert_grounded(plan, {f.id for f in facts})
    experience = next(s for s in plan.sections if s.heading == "Experience")
    assert experience.items
    assert report.rewritten == 0


async def test_only_relevant_experience_is_featured(facts, router, provider):
    provider.replies = ["Senior AI Engineer building RAG systems."]
    plan, report = await write(facts, router)

    experience = next(s for s in plan.sections if s.heading == "Experience")
    featured = " ".join(i.text for i in experience.items)
    assert "Fitly" in featured
    assert any("Fitly" in f for f in report.featured)


async def test_the_report_says_where_the_model_was_overruled(facts, router, provider):
    provider.replies = ["Kubernetes and Rust expert."]
    _, report = await write(facts, router)

    assert report.rejected
    assert any("rejected" in n for n in report.notes)
