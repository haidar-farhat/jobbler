"""Document generation through the HTTP API, including real PDF output."""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pypdf import PdfReader

JOB_DESCRIPTION = """
Requirements:
- Strong Python and FastAPI
- Hands-on RAG
- Docker

Nice to have:
- Kubernetes
"""


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
async def seeded(client):
    """A profile with accepted facts, plus one proposal that must never be used."""
    await client.post("/profile", json={"full_name": "Haidar Farhat", "email": "h@example.com"})

    accepted = [
        ("full_name", "Haidar Farhat", "identity"),
        ("email", "haidar@example.com", "identity"),
        ("current_title", "Senior AI Engineer", "identity"),
        ("Python", "Python", "skill"),
        ("FastAPI", "FastAPI", "skill"),
        ("RAG", "RAG", "skill"),
        ("Docker", "Docker", "skill"),
        ("Laravel", "Laravel", "skill"),
        ("Fitly", "Senior AI Engineer, Fitly - RAG pipeline with FastAPI", "experience"),
    ]
    for key, value, category in accepted:
        await client.post(
            "/profile/facts",
            json={"key": key, "value": value, "category": category, "status": "accepted"},
        )

    # A proposal: present in the profile, but not accepted.
    await client.post(
        "/profile/facts",
        json={"key": "Kubernetes", "value": "Kubernetes", "category": "skill",
              "status": "proposed"},
    )
    return True


async def generate(client, **kwargs):
    body = {"kind": "tailored_cv", "job_title": "AI Engineer", "company": "Northwind",
            "description": JOB_DESCRIPTION, "pdf": False}
    body.update(kwargs)
    return await client.post("/generate", json=body)


# --------------------------------------------------------------------------------------


async def test_generation_needs_accepted_facts(client):
    await client.post("/profile", json={"full_name": "Empty", "email": "e@example.com"})
    response = await generate(client)
    assert response.status_code == 400
    assert "accepted facts" in response.json()["detail"]


async def test_master_cv_generates(client, seeded):
    response = await generate(client, kind="master_cv", job_title=None)
    assert response.status_code == 201

    body = response.json()
    assert body["kind"] == "master_cv"
    assert body["version"] == 1
    assert body["facts_used"] >= 8


async def test_tailored_cv_reports_the_match(client, seeded):
    body = (await generate(client)).json()

    assert body["match_score"] > 0.8
    assert body["match"]["recommendation"] == "apply"
    assert "Kubernetes" in body["match"]["missing_optional"]


async def test_proposed_facts_never_appear_in_output(client, seeded):
    """Kubernetes is proposed, not accepted, and the job asks for it. It must be absent."""
    document_id = (await generate(client)).json()["id"]
    html = (await client.get(f"/generate/{document_id}/preview")).text

    assert "Kubernetes" not in html
    assert "Python" in html


async def test_versions_increment_and_never_overwrite(client, seeded):
    first = (await generate(client)).json()
    second = (await generate(client)).json()

    assert first["version"] == 1
    assert second["version"] == 2
    assert first["id"] != second["id"]

    listing = (await client.get("/generate")).json()
    assert len(listing) == 2


async def test_cover_letter_generates_and_reads_as_a_letter(client, seeded):
    body = (await generate(client, kind="cover_letter")).json()
    html = (await client.get(f"/generate/{body['id']}/preview")).text

    assert "Dear Hiring Team" in html
    assert "AI Engineer" in html
    assert "Haidar Farhat" in html


async def test_tailored_cv_requires_a_job_title(client, seeded):
    response = await generate(client, job_title=None)
    assert response.status_code == 400
    assert "job_title" in response.json()["detail"]


async def test_unknown_kind_is_refused(client, seeded):
    response = await generate(client, kind="autobiography")
    assert response.status_code == 400


async def test_provenance_lists_the_backing_facts(client, seeded):
    document_id = (await generate(client)).json()["id"]
    body = (await client.get(f"/generate/{document_id}/provenance")).json()

    assert body["stale_facts"] == 0
    assert all(f["still_accepted"] for f in body["facts"])
    assert any(f["key"] == "Python" for f in body["facts"])


async def test_provenance_flags_a_fact_that_changed_after_generation(client, seeded):
    """A document you already sent does not update itself. Say so rather than implying it
    still reflects the current profile."""
    document_id = (await generate(client)).json()["id"]

    facts = (await client.get("/profile")).json()["facts"]
    python = next(f for f in facts if f["key"] == "Python")
    await client.post(f"/profile/facts/{python['id']}/reject")

    body = (await client.get(f"/generate/{document_id}/provenance")).json()
    assert body["stale_facts"] == 1
    assert "no longer reflects" in body["note"]


@pytest.mark.browser
async def test_pdf_is_a_real_pdf(client, seeded):
    body = (await generate(client, pdf=True)).json()
    if body.get("pdf_error"):
        pytest.skip(f"Chromium unavailable: {body['pdf_error']}")

    assert body["has_pdf"] is True

    response = await client.get(f"/generate/{body['id']}/pdf")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF-")

    # Readable, paginated, and carrying the actual content -- not an empty shell.
    import io

    reader = PdfReader(io.BytesIO(response.content))
    assert len(reader.pages) >= 1
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    assert "Haidar Farhat" in text
    assert "Python" in text
    assert "Kubernetes" not in text


async def test_pdf_download_404s_when_none_was_rendered(client, seeded):
    body = (await generate(client, pdf=False)).json()
    assert body["has_pdf"] is False
    assert (await client.get(f"/generate/{body['id']}/pdf")).status_code == 404
