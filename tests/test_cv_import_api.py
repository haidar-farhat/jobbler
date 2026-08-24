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
