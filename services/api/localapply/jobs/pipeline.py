"""The pre-browser half of the application state machine.

`applications.state` is written by exactly two wrappers: `advance()` here, and
`RunManager._transition` once a browser is involved. Both call
`state_machine.transition()` first, so an illegal move raises instead of being written.

`advance()` deliberately has **no tolerant flag**. The run loop needs one -- a browser run
can legitimately re-enter a state it is already in -- but a pipeline step cannot: every step
here is a distinct HTTP request, and a request asking for a move that is not legal is a
mistake the caller should be told about, not a silent no-op.
"""

from __future__ import annotations

import copy

from sqlalchemy.ext.asyncio import AsyncSession

from ..db import models as m
from ..db.models import utc_now
from ..documents.cv_parser import KNOWN_SKILLS
from ..documents.matching import extract_requirements, match
from ..orchestrator.state_machine import ApplicationState, transition
from ..profile.store import accepted_skills

S = ApplicationState

#: States a BLOCKED job may be sent back to. Everything from the browser half is excluded:
#: a stalled run is restarted with a fresh `POST /jobs/{id}/apply`, not by dropping the row
#: back into the middle of a loop that is no longer running.
RESUMABLE: frozenset[ApplicationState] = frozenset({
    S.DISCOVERED, S.PARSED, S.ANALYZED, S.SCORED, S.RECOMMENDED,
    S.USER_APPROVED, S.DOCUMENTS_GENERATING, S.READY_FOR_BROWSER,
})

_KNOWN = frozenset(KNOWN_SKILLS)


class WrongState(RuntimeError):
    """The step is legal in the machine, but not from where this job currently is."""

    def __init__(self, current: str, expected: tuple[str, ...]) -> None:
        wanted = " or ".join(expected)
        super().__init__(f"This job is {current}; that step needs it to be {wanted}.")
        self.current = current
        self.expected = expected


def require_state(application: m.Application, *allowed: ApplicationState) -> None:
    if ApplicationState(application.state) not in allowed:
        raise WrongState(application.state, tuple(s.value for s in allowed))


async def advance(
    session: AsyncSession,
    application: m.Application,
    target: ApplicationState,
    *,
    actor: str = "user",
    source: str = "web",
    detail: dict | None = None,
) -> m.Application:
    """Move one application one step, and record that it happened.

    The audit row is not decoration: it is what lets `GET /jobs/{id}` show a real history,
    and what makes "which step went wrong" answerable after the fact rather than guessable.
    """
    current = ApplicationState(application.state)
    application.state = transition(current, target).value  # raises InvalidTransition
    application.updated_at = utc_now()
    session.add(application)
    session.add(
        m.AuditLog(
            actor=actor,
            source=source,
            action="application.transition",
            target=str(application.id),
            detail={
                "from": current.value,
                "to": target.value,
                "job_id": str(application.job_id),
                **(detail or {}),
            },
        )
    )
    await session.commit()
    return application


# --------------------------------------------------------------------------------------
# Creating a job
# --------------------------------------------------------------------------------------


async def create_job(
    session: AsyncSession,
    profile: m.Profile,
    *,
    url: str,
    title: str,
    company: str | None,
    location: str | None,
    description: str,
    source: str,
    external_id: str | None,
) -> tuple[m.Job, m.Application]:
    """Add a posting and the application that tracks it.

    The application's state comes from the column default, not from an argument. Creating a
    row is not a transition -- there is nothing to transition *from* -- and passing
    `state=` here is exactly how the state column acquired a second writer last time.
    """
    job = m.Job(
        url=url,
        title=title,
        company=company,
        location=location,
        description=description or None,
        source=source,
        external_id=external_id,
    )
    session.add(job)
    await session.commit()

    application = m.Application(job_id=job.id, profile_id=profile.id)
    session.add(application)
    await session.commit()
    return job, application


# --------------------------------------------------------------------------------------
# The deterministic middle: extract, score, recommend
# --------------------------------------------------------------------------------------


def assert_closed_vocabulary(rows: list[dict]) -> None:
    """Prove that what is about to be stored contains no posting text.

    `jobs.requirements` is JSON, so anything could be written into it, and a stored blob is
    read back by code that has forgotten where it came from. Every entry must be a name from
    the fixed vocabulary plus a boolean -- which turns "is this data or is this instruction?"
    from a judgement call into an assertion that runs before the commit.
    """
    for row in rows:
        if set(row) != {"skill", "required"}:
            raise ValueError(f"requirement has unexpected keys: {sorted(row)}")
        if row["skill"] not in _KNOWN:
            raise ValueError(
                f"requirement names something outside the vocabulary: {row['skill']!r}"
            )
        if not isinstance(row["required"], bool):
            raise ValueError(f"requirement flag is not a bool: {row['required']!r}")


async def analyze_and_score(
    session: AsyncSession,
    job: m.Job,
    application: m.Application,
    *,
    actor: str = "user",
    source: str = "web",
) -> tuple[list, object]:
    """PARSED -> ANALYZED -> SCORED -> RECOMMENDED, in one request and with no model.

    Three transitions rather than one because each writes a different artefact, and a run
    that dies between them should say which artefact it had produced.

    An empty description is legal and lands on a score of zero with a "skip" recommendation:
    `POST /agent/runs` starts jobs with no description at all, and gating this walk on there
    being text would make the app's oldest entry point illegal.
    """
    description = job.description or ""

    requirements = extract_requirements(description)
    rows = [{"skill": r.skill, "required": r.required} for r in requirements]
    assert_closed_vocabulary(rows)
    # A fresh list: SQLModel does not track in-place mutation of a JSON column, so appending
    # to the existing one would not be written back.
    job.requirements = rows
    session.add(job)
    await session.commit()
    await advance(session, application, S.ANALYZED, actor=actor, source=source,
                  detail={"requirements": len(rows)})

    skills = await accepted_skills(session, application.profile_id)
    result = match(requirements, skills, description)
    job.match_score = result.score
    # `as_dict()` hands back the result's own live lists; storing them without a copy would
    # persist a view of an object that is still being mutated.
    job.match_breakdown = copy.deepcopy(result.as_dict())
    session.add(job)
    await session.commit()
    await advance(session, application, S.SCORED, actor=actor, source=source,
                  detail={"score": result.score})

    # RECOMMENDED persists nothing further: the recommendation is already inside
    # match_breakdown. "apply" and "skip" both land here on purpose -- the recommendation is
    # never encoded in the state, so no automated path can branch on it.
    await advance(session, application, S.RECOMMENDED, actor=actor, source=source,
                  detail={"recommendation": result.recommendation})
    return requirements, result


async def resume_from_blocked(
    session: AsyncSession,
    application: m.Application,
    *,
    actor: str = "user",
    source: str = "web",
) -> ApplicationState:
    """Send a blocked job back to where it was, and only to where it was.

    The target comes from the stored `resume_state`, never from the request: letting a
    caller name it would make "unblock" a way to jump the queue.
    """
    raw = application.resume_state
    target = ApplicationState(raw) if raw in {s.value for s in ApplicationState} else None
    if target is None or target not in RESUMABLE:
        raise WrongState(
            application.state,
            ("a job with a recorded resume point -- cancel this one and add it again",),
        )

    await advance(session, application, S.USER_INTERVENTION, actor=actor, source=source)
    await advance(session, application, target, actor=actor, source=source,
                  detail={"resumed": True})
    application.resume_state = None
    session.add(application)
    await session.commit()
    return target


async def block(
    session: AsyncSession,
    application: m.Application,
    *,
    resume_to: ApplicationState,
    reason: str,
    actor: str = "system",
    source: str = "web",
) -> None:
    """Park a job for a human, remembering where it should come back to."""
    application.resume_state = resume_to.value
    session.add(application)
    await session.flush()
    await advance(session, application, S.BLOCKED, actor=actor, source=source,
                  detail={"reason": reason, "resume_state": resume_to.value})


# --------------------------------------------------------------------------------------
# Serialising
# --------------------------------------------------------------------------------------


def job_view(
    job: m.Job,
    application: m.Application,
    *,
    documents: list | None = None,
    runs: list | None = None,
    history: list | None = None,
    full: bool = False,
) -> dict:
    """One serialiser, so every route describes a job the same way.

    `untrusted` is emitted deliberately: a client rendering these fields is rendering text
    a stranger wrote, and the response says so rather than leaving it to be remembered.
    """
    breakdown = job.match_breakdown or {}
    view = {
        "job_id": str(job.id),
        "application_id": str(application.id),
        "url": job.url,
        "title": job.title,
        "company": job.company,
        "location": job.location,
        "source": job.source,
        "external_id": job.external_id,
        "state": application.state,
        "resume_state": application.resume_state,
        "match_score": job.match_score,
        "recommendation": breakdown.get("recommendation"),
        "matched_required": breakdown.get("matched_required", []),
        "missing_required": breakdown.get("missing_required", []),
        "matched_optional": breakdown.get("matched_optional", []),
        "missing_optional": breakdown.get("missing_optional", []),
        "years_asked": breakdown.get("years_asked"),
        "has_description": bool(job.description),
        "description_chars": len(job.description or ""),
        "requirements": job.requirements or [],
        "documents": len(documents or []),
        "discovered_at": job.discovered_at.isoformat(),
        "updated_at": application.updated_at.isoformat(),
        "submitted_at": application.submitted_at.isoformat() if application.submitted_at else None,
        "simulated": application.simulated,
        "untrusted": ["title", "company", "location", "description", "url"],
    }
    if not full:
        return view

    return {
        **view,
        "description": job.description,
        "match": breakdown,
        "documents": documents or [],
        "runs": runs or [],
        "history": history or [],
    }


def transition_history(rows: list[m.AuditLog]) -> list[dict]:
    return [
        {
            "ts": row.ts.isoformat(),
            "from": row.detail.get("from"),
            "to": row.detail.get("to"),
            "actor": row.actor,
            "source": row.source,
            "detail": {
                k: v for k, v in row.detail.items() if k not in {"from", "to", "job_id"}
            },
        }
        for row in rows
    ]


def run_view(run: m.AgentRun) -> dict:
    return {
        "run_id": str(run.id),
        "status": run.status,
        "goal": run.goal,
        "actions_executed": run.actions_executed,
        "error": run.error,
        "started_at": run.started_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }


__all__ = [
    "RESUMABLE",
    "WrongState",
    "advance",
    "analyze_and_score",
    "assert_closed_vocabulary",
    "block",
    "create_job",
    "job_view",
    "require_state",
    "resume_from_blocked",
    "run_view",
    "transition_history",
]
