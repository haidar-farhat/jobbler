"""Getting your data out, and back in.

Everything a person has -- facts they corrected by hand, documents they actually sent, months
of job history -- lives in one Docker volume that `POST /profile/reset` can destroy in a
single request. Nothing could save it.

The assertion that matters is the round trip. A backup nobody has restored is not a backup,
and every part of this that could be wrong is wrong in a way you only find out about on the
day you need it.
"""

from __future__ import annotations

import io
import json
import zipfile
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from localapply.db import models as m
from localapply.db.session import session_factory
from localapply.portability import FORMAT_VERSION, MANIFEST, BadArchive, import_archive
from sqlmodel import select


@pytest_asyncio.fixture
async def client(settings):
    from localapply.events.bus import EventBus
    from localapply.main import create_app

    app = create_app()
    app.state.settings = settings
    app.state.bus = EventBus()
    app.state.runs = None

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def populated(client):
    """A profile with something worth losing."""
    await client.post("/profile", json={"full_name": "Haidar Farhat", "email": "h@example.com"})
    for key, value, category in [
        ("full_name", "Haidar Farhat", "identity"),
        ("email", "haidar@example.com", "identity"),
        ("Python", "Python", "skill"),
        ("FastAPI", "FastAPI", "skill"),
        ("Docker", "Docker", "skill"),
    ]:
        await client.post(
            "/profile/facts",
            json={"key": key, "value": value, "category": category, "status": "accepted"},
        )
    # An entry corrected by hand -- the thing that is genuinely irreplaceable.
    entry = (await client.post(
        "/profile/facts",
        json={"key": "Dev, Carepool", "value": "Dev, Carepool", "category": "experience",
              "status": "accepted"},
    )).json()
    await client.patch(
        f"/profile/facts/{entry['id']}",
        json={"detail": {"role": "Full-Stack Developer", "organisation": "Carepool",
                         "dates": "Mar 2024 - Present",
                         "bullets": ["Engineered the billing service."]}},
    )

    job = (await client.post("/jobs", json={
        "url": "https://example.com/jobs/1", "title": "AI Engineer", "company": "Northwind",
        "description": "Requirements:\n- Python\n- FastAPI\n- Docker\n" + "Detail. " * 30,
    })).json()
    await client.post(f"/jobs/{job['job_id']}/analyze", json={})
    await client.post(f"/jobs/{job['job_id']}/approve", json={"confirm": True})
    await client.post(f"/jobs/{job['job_id']}/documents",
                      json={"kinds": ["tailored_cv"], "pdf": False})
    await client.post("/searches", json={"source": "greenhouse", "handle": "vercel"})
    return job


async def export_bytes(client) -> bytes:
    response = await client.get("/backup/export")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    return response.content


async def counts() -> dict[str, int]:
    async with session_factory()() as session:
        return {
            "facts": len((await session.execute(select(m.ProfileFact))).scalars().all()),
            "jobs": len((await session.execute(select(m.Job))).scalars().all()),
            "applications": len((await session.execute(select(m.Application))).scalars().all()),
            "documents": len(
                (await session.execute(select(m.GeneratedDocument))).scalars().all()
            ),
            "searches": len((await session.execute(select(m.SavedSearch))).scalars().all()),
            "audit": len((await session.execute(select(m.AuditLog))).scalars().all()),
        }


# --------------------------------------------------------------------------------------
# The archive
# --------------------------------------------------------------------------------------


async def test_the_archive_is_readable_without_this_app(client, populated):
    """A zip of plain JSON can be opened in ten years by someone who has never heard of
    LocalApply. A pg_dump cannot, without the right Postgres."""
    archive = zipfile.ZipFile(io.BytesIO(await export_bytes(client)))

    manifest = json.loads(archive.read(MANIFEST))
    assert manifest["format_version"] == FORMAT_VERSION
    assert manifest["application"] == "LocalApply"

    for name in ("profiles", "profile_facts", "jobs", "applications", "generated_documents"):
        rows = json.loads(archive.read(f"{name}.json"))
        assert isinstance(rows, list)


async def test_the_manifest_says_what_is_deliberately_absent(client, populated):
    archive = zipfile.ZipFile(io.BytesIO(await export_bytes(client)))
    note = json.loads(archive.read(MANIFEST))["note"]
    assert "screenshots" in note
    assert "would have to redo" in note


async def test_a_preview_says_what_is_at_stake_without_downloading_it(client, populated):
    """So "back up first" can show what would be lost rather than asking for faith."""
    preview = (await client.get("/backup/preview")).json()
    assert preview["total"] > 0
    assert preview["rows"]["profile_facts"] >= 6


# --------------------------------------------------------------------------------------
# The round trip
# --------------------------------------------------------------------------------------


async def test_export_wipe_import_leaves_everything_identical(client, populated, settings):
    """The assertion this whole module exists for."""
    before = await counts()
    blob = await export_bytes(client)

    await client.post("/profile/reset", json={"confirm": "DELETE MY DATA"})
    assert (await counts())["facts"] == 0

    async with session_factory()() as session:
        report = await import_archive(session, blob, settings=settings, replace=True)

    assert await counts() == before
    assert report.rows["profile_facts"] == before["facts"]


async def test_a_hand_corrected_entry_survives_exactly(client, populated, settings):
    """The genuinely irreplaceable part: the parser cannot reproduce this, because the CV
    it came from never had a readable date range."""
    blob = await export_bytes(client)
    await client.post("/profile/reset", json={"confirm": "DELETE MY DATA"})

    async with session_factory()() as session:
        await import_archive(session, blob, settings=settings, replace=True)

    facts = (await client.get("/profile")).json()["facts"]
    entry = next(f for f in facts if f["category"] == "experience")
    assert entry["detail"]["dates"] == "Mar 2024 - Present"
    assert entry["detail"]["bullets"] == ["Engineered the billing service."]
    assert entry["source"] == "manual", "an edited fact stays yours across a restore"


async def test_the_audit_trail_survives(client, populated, settings):
    """Which step ran when, months later, is exactly the thing you cannot reconstruct."""
    blob = await export_bytes(client)
    await client.post("/profile/reset", json={"confirm": "DELETE MY DATA"})

    async with session_factory()() as session:
        await import_archive(session, blob, settings=settings, replace=True)

    detail = (await client.get(f"/jobs/{populated['job_id']}")).json()
    walked = [(row["from"], row["to"]) for row in detail["history"]]
    assert ("discovered", "parsed") in walked
    assert ("recommended", "user_approved") in walked


async def test_identifiers_come_back_as_identifiers_not_strings(client, populated, settings):
    """A UUID that restores as its own text is a foreign key that no longer joins."""
    blob = await export_bytes(client)
    await client.post("/profile/reset", json={"confirm": "DELETE MY DATA"})

    async with session_factory()() as session:
        await import_archive(session, blob, settings=settings, replace=True)
        job = (await session.execute(select(m.Job))).scalars().first()
        application = (await session.execute(select(m.Application))).scalars().first()

    assert isinstance(job.id, UUID)
    assert application.job_id == job.id, "the join still holds"


async def test_timestamps_come_back_comparable(client, populated, settings):
    """SQLite hands back naive datetimes from timezone-aware columns, so an export taken
    there and restored to Postgres would carry naive values into a column expecting offsets."""
    from localapply.portability import _decode

    decoded = _decode({"__datetime__": "2026-08-25T10:00:00"})
    assert decoded.tzinfo is not None


# --------------------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------------------


async def test_importing_onto_a_populated_profile_is_refused(client, populated, settings):
    """Merging two identities is not a thing this app has a concept of, and two half-merged
    profiles with facts pointing at the wrong one is worse than being told no."""
    blob = await export_bytes(client)

    async with session_factory()() as session:
        with pytest.raises(BadArchive) as caught:
            await import_archive(session, blob, settings=settings)
    assert "already a profile" in str(caught.value)


async def test_replace_is_an_explicit_choice(client, populated, settings):
    blob = await export_bytes(client)
    async with session_factory()() as session:
        report = await import_archive(session, blob, settings=settings, replace=True)
    assert any("replaced" in note for note in report.notes)


async def test_a_file_that_is_not_an_archive_is_refused(client, settings):
    async with session_factory()() as session:
        with pytest.raises(BadArchive) as caught:
            await import_archive(session, b"not a zip", settings=settings)
    assert "not a LocalApply archive" in str(caught.value)


async def test_a_zip_that_is_not_ours_is_refused(client, settings):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("holiday.jpg", b"not really")

    async with session_factory()() as session:
        with pytest.raises(BadArchive) as caught:
            await import_archive(session, buffer.getvalue(), settings=settings)
    assert "no manifest" in str(caught.value)


async def test_a_future_format_is_refused_rather_than_half_read(client, settings):
    """Importing what it can and stopping would leave a profile with some of its facts."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(MANIFEST, json.dumps({"format_version": FORMAT_VERSION + 5}))

    async with session_factory()() as session:
        with pytest.raises(BadArchive) as caught:
            await import_archive(session, buffer.getvalue(), settings=settings)
    assert "restore half of it" in str(caught.value)


async def test_an_empty_upload_is_refused_by_the_route(client):
    response = await client.post(
        "/backup/import", files={"file": ("empty.zip", b"", "application/zip")}
    )
    assert response.status_code == 400


async def test_a_bad_archive_through_the_route_is_a_400_not_a_500(client):
    response = await client.post(
        "/backup/import", files={"file": ("x.zip", b"nope", "application/zip")}
    )
    assert response.status_code == 400
    assert "archive" in response.json()["detail"].lower()
