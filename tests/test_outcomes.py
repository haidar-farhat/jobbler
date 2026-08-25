"""What happened after you applied.

The application state machine ends at SUBMITTED. Everything a person actually cares about
happens after that, and none of it was recorded -- so the app could apply to two hundred jobs
and never answer the one question it exists to answer.

The load-bearing assertion is that recording an outcome does **not** touch
`applications.state`. That column has exactly two writers and a test that greps for a third;
an outcome is what the employer did, weeks later, and is not something the machine can
validate.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from localapply.db import models as m
from localapply.db.session import session_factory
from localapply.jobs import outcomes as O
from localapply.orchestrator.state_machine import ApplicationState as S
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
async def profile(client):
    await client.post("/profile", json={"full_name": "Haidar Farhat", "email": "h@example.com"})
    return True


async def submitted_job(client, *, score: float = 0.8, company: str = "Northwind",
                        days_ago: int = 10, source: str = "manual") -> dict:
    """A job that has actually been applied for. The state is set directly here rather than
    walked, because this file is about what happens *after* the machine is done."""
    job = (await client.post("/jobs", json={
        "url": f"https://example.com/{company}/{score}/{days_ago}",
        "title": "AI Engineer", "company": company,
    })).json()

    async with session_factory()() as session:
        row = await session.get(m.Job, __import__("uuid").UUID(job["job_id"]))
        row.match_score = score
        row.source = source
        session.add(row)
        application = await session.get(
            m.Application, __import__("uuid").UUID(job["application_id"])
        )
        application.state = S.SUBMITTED.value
        application.submitted_at = datetime.now(UTC) - timedelta(days=days_ago)
        session.add(application)
        await session.commit()
    return job


# --------------------------------------------------------------------------------------
# Recording
# --------------------------------------------------------------------------------------


async def test_an_outcome_can_be_recorded_once_an_application_is_sent(client, profile):
    job = await submitted_job(client)
    response = await client.post(
        f"/jobs/{job['job_id']}/outcome", json={"kind": "replied", "note": "Screening call"}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "replied"
    assert body["heard_back"] is True
    assert body["events"][0]["note"] == "Screening call"


async def test_recording_an_outcome_never_writes_the_state_column(client, profile):
    """The invariant. `applications.state` has exactly two writers and this is not one."""
    job = await submitted_job(client)
    await client.post(f"/jobs/{job['job_id']}/outcome", json={"kind": "rejected"})

    async with session_factory()() as session:
        application = await session.get(
            m.Application, __import__("uuid").UUID(job["application_id"])
        )
    assert application.state == S.SUBMITTED.value


async def test_there_is_nothing_to_record_before_it_is_sent(client, profile):
    job = (await client.post("/jobs", json={
        "url": "https://example.com/x", "title": "AI Engineer",
    })).json()
    response = await client.post(f"/jobs/{job['job_id']}/outcome", json={"kind": "replied"})

    assert response.status_code == 409
    assert "actually been sent" in response.json()["detail"]


async def test_an_unknown_outcome_is_refused(client, profile):
    job = await submitted_job(client)
    response = await client.post(f"/jobs/{job['job_id']}/outcome", json={"kind": "vibes"})
    assert response.status_code == 400


async def test_ghosting_cannot_be_typed_in(client, profile):
    """It is what silence already means, and a reply on week six simply undoes it. Storing
    it would make that reversal a data-repair job."""
    job = await submitted_job(client)
    response = await client.post(f"/jobs/{job['job_id']}/outcome", json={"kind": "ghosted"})

    assert response.status_code == 400
    assert "silence" in response.json()["detail"]


async def test_the_history_is_kept_not_overwritten(client, profile):
    """"Applied, heard back on day nine, screened, interviewed, rejected" is the shape of a
    real process. A single status column answers none of the questions worth asking."""
    job = await submitted_job(client)
    for kind in ("replied", "screening", "interviewed", "rejected"):
        await client.post(f"/jobs/{job['job_id']}/outcome", json={"kind": kind})

    body = (await client.get(f"/jobs/{job['job_id']}")).json()["outcome"]
    assert [e["kind"] for e in body["events"]] == [
        "replied", "screening", "interviewed", "rejected"
    ]
    assert body["status"] == "rejected"
    # How far it got, separately from how it ended.
    assert body["furthest"] == "interviewed"


async def test_when_it_happened_is_not_when_you_typed_it(client, profile):
    """Recording last week's rejection today would otherwise make every response time a lie."""
    job = await submitted_job(client, days_ago=20)
    happened = (datetime.now(UTC) - timedelta(days=14)).isoformat()

    await client.post(
        f"/jobs/{job['job_id']}/outcome",
        json={"kind": "replied", "occurred_at": happened},
    )
    body = (await client.get(f"/jobs/{job['job_id']}")).json()["outcome"]

    # Sent 20 days ago, replied 14 days ago: six days, not zero.
    assert body["days_to_reply"] == pytest.approx(6.0, abs=0.2)


# --------------------------------------------------------------------------------------
# Silence
# --------------------------------------------------------------------------------------


def story(days_ago: int, *kinds: str) -> O.Story:
    submitted = datetime.now(UTC) - timedelta(days=days_ago)
    events = [
        m.ApplicationOutcome(
            application_id=__import__("uuid").uuid4(),
            kind=kind,
            occurred_at=submitted + timedelta(days=i + 1),
        )
        for i, kind in enumerate(kinds)
    ]
    return O.Story(application_id=__import__("uuid").uuid4(), events=events,
                   submitted_at=submitted)


def test_silence_past_five_weeks_reads_as_ghosted():
    assert story(40).status == "ghosted"
    assert story(20).status == "applied"


def test_a_late_reply_undoes_the_ghosting_with_no_repair():
    """Derived, never stored -- which is exactly what makes this free."""
    assert story(40, "replied").status == "replied"


def test_a_closed_application_is_never_called_ghosted():
    assert story(40, "rejected").status == "rejected"
    assert story(40, "withdrawn").status == "withdrawn"


def test_furthest_ignores_how_it_ended():
    """A rejection after an interview got further than a rejection after a screen, and a
    ranking that includes the endings would call the first a step backwards."""
    assert story(10, "replied", "interviewed", "rejected").furthest == "interviewed"
    assert story(10, "replied", "rejected").furthest == "replied"


# --------------------------------------------------------------------------------------
# Is this working?
# --------------------------------------------------------------------------------------


async def test_the_stats_answer_the_question_the_app_exists_to_answer(client, profile):
    """Does a better match actually get replies?"""
    for i in range(6):
        job = await submitted_job(client, score=0.9, company=f"High{i}")
        await client.post(f"/jobs/{job['job_id']}/outcome", json={"kind": "replied"})
    for i in range(6):
        await submitted_job(client, score=0.2, company=f"Low{i}")

    stats = (await client.get("/jobs/stats/outcomes")).json()

    high = next(b for b in stats["by_match_score"] if b["group"] == "80%+")
    low = next(b for b in stats["by_match_score"] if b["group"] == "under 40%")
    assert high["reply_rate"] == 1.0
    assert low["reply_rate"] == 0.0
    assert stats["overall"]["sent"] == 12
    assert stats["overall"]["heard_back"] == 6


async def test_stats_only_count_applications_actually_sent(client, profile):
    """A reply rate across jobs you never applied for is very flattering and completely
    meaningless."""
    await submitted_job(client)
    await client.post("/jobs", json={"url": "https://example.com/never", "title": "Other"})

    stats = (await client.get("/jobs/stats/outcomes")).json()
    assert stats["overall"]["sent"] == 1


async def test_a_rate_from_three_applications_is_flagged_as_not_enough(client, profile):
    """Three applications with one reply is not a 33% reply rate, and showing it as one
    invites a decision the data cannot support."""
    for i in range(3):
        job = await submitted_job(client, company=f"Small{i}")
        if i == 0:
            await client.post(f"/jobs/{job['job_id']}/outcome", json={"kind": "replied"})

    stats = (await client.get("/jobs/stats/outcomes")).json()
    assert stats["overall"]["sent"] == 3
    assert stats["enough_to_judge"] is False


async def test_stats_are_empty_rather_than_zero_when_nothing_was_sent(client, profile):
    stats = (await client.get("/jobs/stats/outcomes")).json()
    assert stats["overall"]["sent"] == 0
    # A fraction of nothing is not zero; it is nothing.
    assert stats["overall"]["reply_rate"] is None
    assert stats["overall"]["median_reply_days"] is None


async def test_the_median_reply_time_is_reported(client, profile):
    for days, replied_after in ((30, 5), (30, 10), (30, 21)):
        job = await submitted_job(client, days_ago=days, company=f"C{days}{replied_after}")
        happened = (datetime.now(UTC) - timedelta(days=days - replied_after)).isoformat()
        await client.post(
            f"/jobs/{job['job_id']}/outcome",
            json={"kind": "replied", "occurred_at": happened},
        )

    stats = (await client.get("/jobs/stats/outcomes")).json()
    assert stats["overall"]["median_reply_days"] == pytest.approx(10.0, abs=0.3)


async def test_the_board_shows_the_outcome(client, profile):
    job = await submitted_job(client)
    await client.post(f"/jobs/{job['job_id']}/outcome", json={"kind": "interviewed"})

    board = (await client.get("/jobs")).json()
    row = next(j for j in board["jobs"] if j["job_id"] == job["job_id"])
    assert row["outcome"]["status"] == "interviewed"


async def test_a_job_not_yet_sent_carries_no_outcome(client, profile):
    await client.post("/jobs", json={"url": "https://example.com/x", "title": "AI Engineer"})
    board = (await client.get("/jobs")).json()
    assert board["jobs"][0]["outcome"] is None


async def test_the_whole_board_costs_one_outcome_query(client, profile):
    """Per-row would be one round trip per job, which is the shape of every list page that
    gets slow six months in."""
    for i in range(12):
        await submitted_job(client, company=f"Co{i}")

    async with session_factory()() as session:
        applications = list(
            (await session.execute(select(m.Application))).scalars().all()
        )
        stories = await O.stories_for(session, applications)

    assert len(stories) == 12
    assert all(isinstance(s, O.Story) for s in stories.values())


def test_no_applications_means_no_queries():
    import asyncio

    async def check():
        async with session_factory()() as session:
            return await O.stories_for(session, [])

    assert asyncio.run(check()) == {}
