"""CV import through the HTTP API, against a real database.

The headline assertion is negative: uploading a CV must not change what the agent is willing
to type into an application until a human has accepted each fact individually.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from localapply.api.routes.profile import load_reasoning_context
from localapply.db import models as m
from localapply.db.session import session_factory
from localapply.profile.facts import FactStatus
from sqlmodel import select

FIXTURE = Path(__file__).resolve().parents[1] / "evaluation" / "fixtures" / "sample-cv.txt"


@pytest_asyncio.fixture
async def client(settings):
    """The app without its lifespan: the database is already set up by the `database`
    fixture, and no browser is needed for document import."""
    from localapply.events.bus import EventBus
    from localapply.main import create_app

    app = create_app()
    app.state.settings = settings
    app.state.bus = EventBus()
    app.state.runs = None  # documents and profile routes never touch the run manager

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def profile(client):
    response = await client.post(
        "/profile", json={"full_name": "Test User", "email": "existing@example.com"}
    )
    assert response.status_code == 201
    return response.json()


def cv_bytes() -> bytes:
    return FIXTURE.read_bytes()


async def upload(client, data: bytes | None = None, filename: str = "cv.txt"):
    return await client.post(
        "/documents",
        files={"file": (filename, data if data is not None else cv_bytes(), "text/plain")},
    )


async def facts(status: str | None = None) -> list[m.ProfileFact]:
    async with session_factory()() as session:
        query = select(m.ProfileFact)
        if status:
            query = query.where(m.ProfileFact.status == status)
        return list((await session.execute(query)).scalars().all())


# --------------------------------------------------------------------------------------


async def test_upload_requires_a_profile(client):
    response = await upload(client)
    assert response.status_code == 400
    assert "profile" in response.json()["detail"].lower()


async def test_upload_extracts_and_proposes(client, profile):
    response = await upload(client)
    assert response.status_code == 201

    body = response.json()
    assert body["document"]["parser"] == "text"
    assert body["pending_facts"] > 10
    assert "identity" in body["by_category"]
    assert "skill" in body["by_category"]


async def test_upload_accepts_nothing_by_itself(client, profile):
    """The core guarantee of the whole phase."""
    await upload(client)

    assert await facts(FactStatus.ACCEPTED.value) == []
    proposed = await facts(FactStatus.PROPOSED.value)
    assert proposed, "the import should have produced proposals"
    assert all(f.source == "cv_import" for f in proposed)


async def test_the_agent_cannot_see_proposed_facts(client, profile):
    """A proposal must be invisible to the reasoner, or 'you approve each fact' is theatre."""
    await upload(client)

    async with session_factory()() as session:
        context = await load_reasoning_context(session, "apply")

    assert context.profile == {}
    assert context.drafts == {}


async def test_accepting_one_fact_makes_exactly_that_fact_usable(client, profile):
    await upload(client)
    proposed = await facts(FactStatus.PROPOSED.value)
    email = next(f for f in proposed if f.key == "email")

    response = await client.post(f"/profile/facts/{email.id}/accept")
    assert response.status_code == 200

    async with session_factory()() as session:
        context = await load_reasoning_context(session, "apply")

    assert context.profile == {"email": "haidar@example.com"}


async def test_rejecting_a_fact_keeps_it_out_and_remembers_it(client, profile):
    await upload(client)
    proposed = await facts(FactStatus.PROPOSED.value)
    email = next(f for f in proposed if f.key == "email")

    await client.post(f"/profile/facts/{email.id}/reject")

    async with session_factory()() as session:
        context = await load_reasoning_context(session, "apply")
    assert "email" not in context.profile

    async with session_factory()() as session:
        row = await session.get(m.ProfileFact, email.id)
    assert row.status == FactStatus.REJECTED.value
    assert row.resolved_at is not None


async def test_reuploading_the_same_file_does_not_duplicate_proposals(client, profile):
    first = await upload(client)
    before = len(await facts())

    second = await upload(client)
    assert second.json()["already_imported"] is True
    assert second.json()["document"]["id"] == first.json()["document"]["id"]
    assert len(await facts()) == before


async def test_a_changed_value_is_flagged_as_a_conflict(client, profile):
    """Accept an email, then import a CV with a different one. The old value must survive
    until the replacement is explicitly accepted."""
    await client.post(
        "/profile/facts",
        json={"key": "email", "value": "old@example.com", "category": "identity"},
    )
    response = await upload(client)
    document_id = response.json()["document"]["id"]

    listing = (await client.get(f"/documents/{document_id}/facts")).json()
    conflict = next(f for f in listing if f["key"] == "email")

    assert conflict["verdict"] == "conflict"
    assert conflict["replaces"] == "old@example.com"

    # Still the old value, because nothing has been accepted yet.
    async with session_factory()() as session:
        context = await load_reasoning_context(session, "apply")
    assert context.profile["email"] == "old@example.com"

    await client.post(f"/profile/facts/{conflict['id']}/accept")

    async with session_factory()() as session:
        context = await load_reasoning_context(session, "apply")
    assert context.profile["email"] == "haidar@example.com"


async def test_accepting_a_conflict_supersedes_the_old_fact(client, profile):
    original = (
        await client.post(
            "/profile/facts",
            json={"key": "email", "value": "old@example.com", "category": "identity"},
        )
    ).json()

    response = await upload(client)
    listing = (await client.get(f"/documents/{response.json()['document']['id']}/facts")).json()
    conflict = next(f for f in listing if f["key"] == "email")
    await client.post(f"/profile/facts/{conflict['id']}/accept")

    async with session_factory()() as session:
        previous = await session.get(m.ProfileFact, UUID(original["id"]))

    # Superseded, not deleted: the history of what you once claimed is kept.
    assert previous.status == FactStatus.SUPERSEDED.value
    assert previous.value == "old@example.com"


async def test_bulk_accept_never_touches_conflicts(client, profile):
    await client.post(
        "/profile/facts",
        json={"key": "email", "value": "old@example.com", "category": "identity"},
    )
    response = await upload(client)
    document_id = response.json()["document"]["id"]

    result = (await client.post(f"/documents/{document_id}/accept-all")).json()
    assert result["accepted"] > 0
    assert result["still_pending"] >= 1

    remaining = await facts(FactStatus.PROPOSED.value)
    assert all(f.supersedes_id is not None for f in remaining), (
        "only conflicts should be left pending after a bulk accept"
    )

    async with session_factory()() as session:
        context = await load_reasoning_context(session, "apply")
    assert context.profile["email"] == "old@example.com"


async def test_bulk_accept_can_be_narrowed_by_category_and_confidence(client, profile):
    document_id = (await upload(client)).json()["document"]["id"]

    result = (
        await client.post(
            f"/documents/{document_id}/accept-all",
            params={"category": "skill", "min_confidence": 0.8},
        )
    ).json()
    assert result["accepted"] > 0

    accepted = await facts(FactStatus.ACCEPTED.value)
    assert all(f.category == "skill" and f.confidence >= 0.8 for f in accepted)


async def test_unreadable_upload_is_rejected_with_a_reason(client, profile):
    response = await upload(client, data=b"tiny", filename="scan.txt")
    assert response.status_code == 422
    assert "characters" in response.json()["detail"] or "scan" in response.json()["detail"]

    # The failed attempt is recorded so the user can see what was tried.
    async with session_factory()() as session:
        documents = list((await session.execute(select(m.Document))).scalars().all())
    assert any(d.error for d in documents)


async def test_extracted_text_is_retrievable_for_auditing(client, profile):
    document_id = (await upload(client)).json()["document"]["id"]
    body = (await client.get(f"/documents/{document_id}/text")).json()
    assert "Haidar Farhat" in body["text"]


async def test_summary_reports_what_the_parser_could_not_find(client, profile):
    document_id = (await upload(client)).json()["document"]["id"]
    summary = (await client.get(f"/documents/{document_id}/summary")).json()

    assert summary["extracted"] > 10
    assert "skills" in summary["sections"]
    assert "counts" in summary


@pytest.mark.parametrize("endpoint", ["accept", "reject"])
async def test_unknown_fact_is_a_404(client, profile, endpoint):
    from uuid import uuid4

    response = await client.post(f"/profile/facts/{uuid4()}/{endpoint}")
    assert response.status_code == 404


# --------------------------------------------------------------------------------------
# Refusing the app's own output
#
# A user uploaded a LocalApply-generated CV back into LocalApply. The parser treated the
# generated prose as source facts -- including the generator's own footer, which became an
# "experience" bullet reading "Generated by LocalApply ... from 61 verified profile facts"
# and then appeared on the next CV. Each round was built from the round before.
# --------------------------------------------------------------------------------------


async def test_a_generated_cv_is_refused_by_its_footer(client, profile):
    generated = (
        "Haydar Farhat\nAI Engineer\nSUMMARY\nSome generated prose about the candidate "
        "that is long enough to clear the extraction floor and look like a real document. "
        "EXPERIENCE\nFull-Stack Developer, Carepool\n"
        "Generated by LocalApply on 24 August 2026 from 61 verified profile facts.\n"
    )
    response = await upload(client, data=generated.encode(), filename="master_cv-v6.txt")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "LocalApply generated" in detail
    assert "own output" in detail


async def test_refusing_a_generated_cv_leaves_the_profile_untouched(client, profile):
    before = len(await facts())
    generated = (
        "Some generated document text long enough to pass extraction checks and then some "
        "more words to be safe.\nGenerated by LocalApply on 24 August 2026 from 12 "
        "verified profile facts.\n"
    )
    await upload(client, data=generated.encode(), filename="cv.txt")

    assert len(await facts()) == before, "a refused upload must not propose anything"


async def test_a_real_cv_is_still_accepted(client, profile):
    """The guard must not become a general refusal."""
    assert (await upload(client)).status_code == 201


# --------------------------------------------------------------------------------------
# Correcting what the parser got wrong
#
# Extraction from arbitrary PDFs is a draft, not an answer. A CV that writes its dates as
# "Full-time | 1 Year" has no date range for any parser to find, and before this the only
# options were to accept a wrong fact or reject it and lose the entry entirely.
# --------------------------------------------------------------------------------------


async def experience_fact(client) -> dict:
    await upload(client)
    proposed = await facts(FactStatus.PROPOSED.value)
    entry = next((f for f in proposed if f.category == "experience"), None)
    assert entry is not None, "the fixture CV should yield at least one experience entry"
    return {"id": str(entry.id), "row": entry}


async def test_editing_a_fact_rewrites_its_structured_detail(client, profile):
    entry = await experience_fact(client)

    response = await client.patch(
        f"/profile/facts/{entry['id']}",
        json={
            "detail": {
                "role": "Full-Stack Developer",
                "organisation": "Carepool",
                "dates": "Jan 2023 - Present",
                "bullets": ["  Shipped   the API  ", "", "Cut latency by 40%"],
            }
        },
    )

    assert response.status_code == 200
    detail = response.json()["detail"]
    assert detail["dates"] == "Jan 2023 - Present"
    # Whitespace is normalised and blank lines dropped, because the editor is a textarea.
    assert detail["bullets"] == ["Shipped the API", "Cut latency by 40%"]


async def test_editing_keeps_the_flat_value_in_step(client, profile):
    """`value` is what the profile table, matching and the cover letter read. If it kept
    the parser's wrong headline after an edit, the correction would only show up on the CV.
    """
    entry = await experience_fact(client)

    body = (
        await client.patch(
            f"/profile/facts/{entry['id']}",
            json={
                "detail": {
                    "role": "Backend Engineer",
                    "organisation": "Acme",
                    "dates": "2024 - 2025",
                }
            },
        )
    ).json()

    assert "Backend Engineer" in body["value"]
    assert "Acme" in body["value"]
    assert "2024 - 2025" in body["value"]


async def test_an_edit_marks_the_fact_as_yours(client, profile):
    entry = await experience_fact(client)

    body = (
        await client.patch(
            f"/profile/facts/{entry['id']}", json={"detail": {"role": "Backend Engineer"}}
        )
    ).json()

    assert body["source"] == "manual", "an edited fact is no longer the import's claim"
    assert body["confidence"] == 1.0


async def test_editing_only_touches_the_keys_you_send(client, profile):
    entry = await experience_fact(client)
    await client.patch(
        f"/profile/facts/{entry['id']}",
        json={"detail": {"role": "Backend Engineer", "bullets": ["Kept this one"]}},
    )

    body = (
        await client.patch(
            f"/profile/facts/{entry['id']}", json={"detail": {"dates": "2020 - 2021"}}
        )
    ).json()

    assert body["detail"]["bullets"] == ["Kept this one"]
    assert body["detail"]["role"] == "Backend Engineer"


async def test_editing_does_not_accept_the_fact(client, profile):
    """Correcting a proposal is not the same as approving it."""
    entry = await experience_fact(client)

    body = (
        await client.patch(
            f"/profile/facts/{entry['id']}", json={"detail": {"role": "Backend Engineer"}}
        )
    ).json()
    assert body["status"] == FactStatus.PROPOSED.value

    async with session_factory()() as session:
        context = await load_reasoning_context(session, "apply")
    assert body["key"] not in context.profile


async def test_an_edited_fact_reaches_the_generator_once_accepted(client, profile):
    entry = await experience_fact(client)
    await client.patch(
        f"/profile/facts/{entry['id']}",
        json={"detail": {"role": "Backend Engineer", "organisation": "Acme",
                         "dates": "2024 - 2025", "bullets": ["Cut latency by 40%"]}},
    )
    await client.post(f"/profile/facts/{entry['id']}/accept")

    async with session_factory()() as session:
        row = await session.get(m.ProfileFact, UUID(entry["id"]))

    assert row.status == FactStatus.ACCEPTED.value
    assert row.detail["dates"] == "2024 - 2025"
    assert row.detail["bullets"] == ["Cut latency by 40%"]


async def test_a_fact_cannot_be_edited_into_nothing(client, profile):
    entry = await experience_fact(client)
    response = await client.patch(f"/profile/facts/{entry['id']}", json={"value": "   "})

    assert response.status_code == 400
    assert "reject" in response.json()["detail"].lower()


async def test_editing_an_unknown_fact_is_a_404(client, profile):
    from uuid import uuid4

    response = await client.patch(f"/profile/facts/{uuid4()}", json={"value": "x"})
    assert response.status_code == 404


# --------------------------------------------------------------------------------------
# Start over
# --------------------------------------------------------------------------------------


async def test_reset_requires_the_exact_phrase(client, profile):
    await upload(client)
    before = len(await facts())

    response = await client.post("/profile/reset", json={"confirm": "yes"})

    assert response.status_code == 400
    assert "DELETE MY DATA" in response.json()["detail"]
    assert len(await facts()) == before, "nothing may be deleted without confirmation"


async def test_reset_clears_facts_and_documents(client, profile):
    await upload(client)
    assert await facts()

    response = await client.post("/profile/reset", json={"confirm": "DELETE MY DATA"})

    assert response.status_code == 200
    assert response.json()["deleted"]["facts"] > 0
    assert await facts() == []

    async with session_factory()() as session:
        documents = list((await session.execute(select(m.Document))).scalars().all())
    assert documents == []


async def test_reset_can_keep_the_uploaded_document(client, profile):
    """After a parser improvement you want the facts gone but the CV kept, so it can be
    re-imported without asking the user to find the file again."""
    await upload(client)

    response = await client.post(
        "/profile/reset", json={"confirm": "DELETE MY DATA", "keep_documents": True}
    )

    assert response.status_code == 200
    assert await facts() == []

    async with session_factory()() as session:
        documents = list((await session.execute(select(m.Document))).scalars().all())
    assert len(documents) == 1, "the source CV should survive"


async def test_reset_is_accepted_when_there_is_nothing_to_delete(client):
    response = await client.post("/profile/reset", json={"confirm": "DELETE MY DATA"})
    assert response.status_code == 200
    assert "No profile" in response.json()["note"]
