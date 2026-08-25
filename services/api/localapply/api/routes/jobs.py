"""The jobs board: add a posting, score it, approve it, prepare it, hand it to the agent.

Every handler is a thin shell -- check the state, call the service, advance the state. The
work lives in `jobs/pipeline.py` and `jobs/ingest.py`, which import no web framework, so the
pipeline stays testable without a client and reusable from somewhere other than HTTP.

Nothing here reads a job's description in a conditional. See `jobs/__init__.py` for why that
is the property the whole design rests on.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ...config import Settings
from ...db import models as m
from ...db.session import get_session
from ...documents.pipeline import (
    JOB_DOCUMENT_KINDS,
    NoAcceptedFacts,
    UngroundedDocument,
    build_document,
)
from ...events.bus import EventBus
from ...jobs import pipeline as P
from ...orchestrator.run_loop import RunManager
from ...orchestrator.state_machine import ApplicationState as S
from ...safety import KILL_SWITCH, AutomationHalted
from ..deps import get_app_settings, get_bus, get_router, get_run_manager
from .generate import _summary as document_summary
from .profile import current_profile, load_reasoning_context

router = APIRouter(prefix="/jobs", tags=["jobs"])

#: What a client may claim a job came from. Free text here would end up rendered on the
#: board, and `source` is one of the few job fields that is *not* meant to be posting text.
JOB_SOURCES = {"manual", "browser", "fixture"}


class JobIn(BaseModel):
    url: str
    title: str = "Untitled role"
    company: str | None = None
    location: str | None = None
    #: Paste the posting here, or leave it empty and use POST /jobs/{id}/ingest.
    description: str = ""
    source: str = "manual"
    external_id: str | None = None


class IngestIn(BaseModel):
    #: "paste" writes what you send. "fetch" opens the URL *you typed* in the browser.
    mode: str = "paste"
    description: str = ""


class AnalyzeIn(BaseModel):
    description: str = ""


class ApproveIn(BaseModel):
    #: Deliberately not a default. Approving is the one step in the pipeline that no
    #: automated path may take, so it takes an explicit act from the caller.
    confirm: bool = False


class DocumentsIn(BaseModel):
    kinds: list[str] = Field(default_factory=lambda: ["tailored_cv"])
    polish: bool = False
    pdf: bool = True


class ApplyIn(BaseModel):
    goal: str = "Complete this job application."
    document_id: UUID | None = None


class CancelIn(BaseModel):
    reason: str = "Not interested"


# --------------------------------------------------------------------------------------


async def _load(session: AsyncSession, job_id: UUID) -> tuple[m.Job, m.Application]:
    job = await session.get(m.Job, job_id)
    if job is None:
        raise HTTPException(404, "No such job.")
    result = await session.execute(
        select(m.Application).where(m.Application.job_id == job_id)
    )
    application = result.scalars().first()
    if application is None:
        raise HTTPException(404, "That job has no application row; add it again.")
    return job, application


async def _documents_for(session: AsyncSession, job_id: UUID) -> list[m.GeneratedDocument]:
    result = await session.execute(
        select(m.GeneratedDocument)
        .where(m.GeneratedDocument.job_id == job_id)
        .order_by(m.GeneratedDocument.created_at.desc())
    )
    return list(result.scalars().all())


def _no_live_run(runs: RunManager | None, application: m.Application) -> None:
    """Refuse to write a job's state from underneath a run that is still using it.

    Without this the terminal row and the live in-memory handle disagree, and the loop's
    next transition silently un-cancels the job.
    """
    live = runs.live_for(application.id) if runs is not None else None
    if live is not None:
        raise HTTPException(
            409,
            f"A run is live for this application (run {live.run_id}). Stop it with "
            f"POST /agent/runs/{live.run_id}/stop first.",
        )


# --------------------------------------------------------------------------------------


@router.post("", status_code=201)
async def add_job(payload: JobIn, session: AsyncSession = Depends(get_session)) -> dict:
    """Add a posting. Never touches the network -- fetching is its own explicit step."""
    profile = await current_profile(session)
    if profile is None:
        raise HTTPException(400, "Create a profile before adding a job.")

    from ...jobs.ingest import UnsafeURL, check_url

    try:
        check_url(payload.url, allow_loopback=True)
    except UnsafeURL as exc:
        raise HTTPException(400, str(exc)) from exc

    source = payload.source if payload.source in JOB_SOURCES else "manual"
    job, application = await P.create_job(
        session,
        profile,
        url=payload.url,
        title=payload.title.strip() or "Untitled role",
        company=payload.company,
        location=payload.location,
        description=payload.description,
        source=source,
        external_id=payload.external_id,
    )
    if payload.description.strip():
        # PARSED means "there is nothing further to fetch or paste for this job".
        await P.advance(session, application, S.PARSED, detail={"via": "create"})

    return P.job_view(job, application)


@router.get("")
async def list_jobs(
    state: str | None = None,
    recommendation: str | None = None,
    min_score: float | None = None,
    source: str | None = None,
    q: str | None = None,
    order: str = "score",
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """The board. Filters are validated against the enums, never interpolated."""
    if state is not None and state not in {s.value for s in S}:
        raise HTTPException(400, f"Unknown state {state!r}.")
    if recommendation is not None and recommendation not in {"apply", "consider", "skip"}:
        raise HTTPException(400, f"Unknown recommendation {recommendation!r}.")

    rows = (
        await session.execute(
            select(m.Job, m.Application).join(m.Application, m.Application.job_id == m.Job.id)
        )
    ).all()

    counts: dict[str, int] = {}
    for _, application in rows:
        counts[application.state] = counts.get(application.state, 0) + 1

    def keep(job: m.Job, application: m.Application) -> bool:
        if state is not None:
            if application.state != state:
                return False
        # A cancelled job is history, not work in hand -- unless it was asked for by name.
        elif application.state == S.CANCELLED.value:
            return False
        # Falls through in both branches: `?state=recommended&min_score=0.9` used to return
        # a job scored 0.1, because naming a state returned early and dropped every other
        # filter on the floor.
        if source is not None and job.source != source:
            return False
        if min_score is not None and (job.match_score or 0.0) < min_score:
            return False
        if (
            recommendation is not None
            and (job.match_breakdown or {}).get("recommendation") != recommendation
        ):
            return False
        if q:
            needle = q.casefold()
            haystack = f"{job.title} {job.company or ''}".casefold()
            if needle not in haystack:
                return False
        return True

    kept = [(job, app) for job, app in rows if keep(job, app)]

    if order == "discovered":
        kept.sort(key=lambda pair: pair[0].discovered_at, reverse=True)
    elif order == "updated":
        kept.sort(key=lambda pair: pair[1].updated_at, reverse=True)
    else:
        # Unscored jobs sort last rather than as zero: "not yet scored" is not "scored zero".
        kept.sort(key=lambda pair: (pair[0].match_score is None, -(pair[0].match_score or 0.0)))

    page = kept[offset : offset + limit]
    documents = {
        job.id: await _documents_for(session, job.id) for job, _ in page
    }
    return {
        "total": len(kept),
        "limit": limit,
        "offset": offset,
        "counts": counts,
        "jobs": [
            P.job_view(job, application, documents=documents[job.id])
            for job, application in page
        ],
    }


@router.get("/{job_id}")
async def read_job(job_id: UUID, session: AsyncSession = Depends(get_session)) -> dict:
    job, application = await _load(session, job_id)
    documents = await _documents_for(session, job_id)
    runs = list(
        (
            await session.execute(
                select(m.AgentRun)
                .where(m.AgentRun.application_id == application.id)
                .order_by(m.AgentRun.started_at.desc())
            )
        ).scalars().all()
    )
    history = list(
        (
            await session.execute(
                select(m.AuditLog)
                .where(
                    m.AuditLog.target == str(application.id),
                    m.AuditLog.action == "application.transition",
                )
                .order_by(m.AuditLog.ts)
            )
        ).scalars().all()
    )
    return P.job_view(
        job,
        application,
        documents=[document_summary(d) for d in documents],
        runs=[P.run_view(r) for r in runs],
        history=P.transition_history(history),
        full=True,
    )


@router.post("/{job_id}/ingest")
async def ingest(
    job_id: UUID,
    payload: IngestIn,
    settings: Settings = Depends(get_app_settings),
    bus: EventBus = Depends(get_bus),
    runs: RunManager = Depends(get_run_manager),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Get the posting's text: paste it, or have the browser read the URL you typed.

    The only route in this module that touches the network, and it is never called
    implicitly by another one -- reading a third-party page is a thing the user asks for.
    """
    job, application = await _load(session, job_id)
    P.require_state(application, S.DISCOVERED)

    if payload.mode == "paste":
        text = payload.description.strip()
        if not text:
            raise HTTPException(400, "mode=paste needs a description.")
        job.description = text
        job.source = "manual"
        session.add(job)
        await session.commit()
        await P.advance(session, application, S.PARSED, detail={"via": "paste"})
        return {
            "state": application.state,
            "source": job.source,
            "description_chars": len(text),
        }

    if payload.mode != "fetch":
        raise HTTPException(400, "mode must be 'paste' or 'fetch'.")

    from ...jobs.ingest import IngestBlocked, UnsafeURL, fetch_description

    if KILL_SWITCH.engaged:
        raise HTTPException(
            409,
            f"Automation is stopped ({KILL_SWITCH.reason}). "
            "Re-arm with POST /agent/kill-switch/reset before fetching a page.",
        )

    try:
        result = await fetch_description(
            browser=runs.browser, bus=bus, settings=settings, job=job,
            application_id=application.id,
        )
    except UnsafeURL as exc:
        raise HTTPException(400, str(exc)) from exc
    except IngestBlocked as exc:
        # A login wall or a CAPTCHA. Hand it to the human; store nothing.
        await P.block(session, application, resume_to=S.DISCOVERED, reason=str(exc))
        return {
            "state": application.state,
            "blocked": True,
            "page_kind": exc.page_kind,
            "resume_state": application.resume_state,
            "next": (
                "Open the URL yourself, get past the wall, then POST /jobs/"
                f"{job_id}/unblock and paste the text with mode=paste."
            ),
        }
    except AutomationHalted as exc:
        # Caught before RuntimeError, which it subclasses. The pre-check above closes the
        # common case, but the switch can be engaged between it and the executor's own
        # check -- and the answer to "automation is stopped" is 409 everywhere else in this
        # API, not the 503 that means "the browser could not be used".
        raise HTTPException(409, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc

    job.description = result.text
    job.source = "browser"
    session.add(job)
    await session.commit()
    await P.advance(session, application, S.PARSED,
                    detail={"via": "fetch", "page_kind": result.page_kind})
    return {
        "state": application.state,
        "source": job.source,
        "description_chars": len(result.text),
        "page_kind": result.page_kind,
        "ingest_run_id": str(result.run_id),
    }


@router.post("/{job_id}/analyze")
async def analyze(
    job_id: UUID, payload: AnalyzeIn, session: AsyncSession = Depends(get_session)
) -> dict:
    """Extract the requirements, score them against your accepted skills, recommend.

    No model, no network, no browser: a regular expression over a fixed vocabulary and some
    arithmetic. A posting cannot argue its way to a higher score.
    """
    job, application = await _load(session, job_id)

    if payload.description.strip():
        P.require_state(application, S.DISCOVERED, S.PARSED)
        job.description = payload.description.strip()
        session.add(job)
        await session.commit()
        if application.state == S.DISCOVERED.value:
            await P.advance(session, application, S.PARSED, detail={"via": "analyze"})
    else:
        P.require_state(application, S.PARSED)

    if not (job.description or "").strip():
        raise HTTPException(400, "Paste or fetch the job description before analysing.")

    requirements, result = await P.analyze_and_score(session, job, application)
    return {
        **P.job_view(job, application),
        # `evidence` is raw posting text. It is returned for audit and deliberately never
        # persisted -- re-running the extractor reproduces it for free.
        "requirements_detail": [
            {"skill": r.skill, "required": r.required, "evidence": r.evidence}
            for r in requirements
        ],
        "match": result.as_dict(),
    }


@router.post("/{job_id}/approve")
async def approve(
    job_id: UUID, payload: ApproveIn, session: AsyncSession = Depends(get_session)
) -> dict:
    """The human gate, and the only caller of RECOMMENDED -> USER_APPROVED anywhere.

    Nothing automated may reach this: a hostile posting raises its own match score simply by
    listing skills the profile has, and this whole design is safe only because no code path
    acts on that number.
    """
    job, application = await _load(session, job_id)
    P.require_state(application, S.RECOMMENDED)
    if not payload.confirm:
        raise HTTPException(400, "Set confirm: true to approve this job.")

    await P.advance(session, application, S.USER_APPROVED, detail={"confirmed": True})
    return {
        **P.job_view(job, application),
        "next": f"POST /jobs/{job_id}/documents",
    }


@router.post("/{job_id}/documents")
async def make_documents(
    job_id: UUID,
    payload: DocumentsIn,
    settings: Settings = Depends(get_app_settings),
    model_router=Depends(get_router),
    runs: RunManager = Depends(get_run_manager),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Write the CV (and optionally the letter) for this posting.

    A document problem is recoverable -- accept some facts, fix an entry, try again -- so it
    parks the job at BLOCKED with somewhere to come back to, rather than failing the request
    or ending the application.
    """
    job, application = await _load(session, job_id)
    P.require_state(application, S.USER_APPROVED)
    _no_live_run(runs, application)

    unknown = [k for k in payload.kinds if k not in JOB_DOCUMENT_KINDS]
    if unknown or not payload.kinds:
        raise HTTPException(
            400,
            f"kinds must be a non-empty subset of {list(JOB_DOCUMENT_KINDS)}; "
            f"a master CV is not written for a posting.",
        )

    profile = await current_profile(session)
    # Committed before any work runs: a row reading "ready_for_browser" while its documents
    # are still being built is a lie the dashboard would show.
    await P.advance(session, application, S.DOCUMENTS_GENERATING)

    built: list[dict] = []
    try:
        for kind in payload.kinds:  # noqa: SIM117 - the try wraps the whole loop deliberately
            result = await build_document(
                session, settings, profile,
                kind=kind,
                job_title=job.title,
                company=job.company,
                description=job.description or "",
                job_id=job.id,
                pdf=payload.pdf,
                polish=payload.polish,
                router=model_router,
            )
            built.append({**document_summary(result.document),
                          "notes": result.notes,
                          "pdf_error": result.pdf_error})
    except (NoAcceptedFacts, UngroundedDocument, ValueError) as exc:
        await P.block(session, application, resume_to=S.USER_APPROVED, reason=str(exc))
        return {
            "state": application.state,
            "resume_state": application.resume_state,
            "error": str(exc),
            "retry": f"POST /jobs/{job_id}/unblock then POST /jobs/{job_id}/documents",
        }

    await P.advance(session, application, S.READY_FOR_BROWSER,
                    detail={"documents": len(built)})
    return {
        "state": application.state,
        "documents": built,
        "match_score": job.match_score,
        "next": f"POST /jobs/{job_id}/apply",
    }


@router.post("/{job_id}/apply", status_code=201)
async def apply(
    job_id: UUID,
    payload: ApplyIn,
    runs: RunManager = Depends(get_run_manager),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Hand the prepared application to the browser agent.

    Writes no state: the run loop owns the browser half, and its first transition is
    READY_FOR_BROWSER -> BROWSER_RUNNING.
    """
    if KILL_SWITCH.engaged:
        raise HTTPException(
            409,
            f"Automation is stopped ({KILL_SWITCH.reason}). "
            "Re-arm with POST /agent/kill-switch/reset before starting a run.",
        )

    job, application = await _load(session, job_id)
    if application.state != S.READY_FOR_BROWSER.value:
        raise HTTPException(
            409,
            f"This job is {application.state}; generate its documents first "
            f"(POST /jobs/{job_id}/documents).",
        )
    # The transition table alone does not prevent two runs on one application -- several
    # states legally lead back into the pipeline -- so the guard is explicit.
    _no_live_run(runs, application)

    document = await _resume_document(session, job, payload.document_id)

    context = await load_reasoning_context(session, payload.goal)
    context.job_title = job.title
    context.company = job.company
    if document is not None and document.pdf_path:
        # The upload action reads this key. Pointing it at the CV written *for this posting*
        # is the whole reason the documents step exists.
        context.profile["resume_path"] = document.pdf_path

    try:
        handle = await runs.start(
            start_url=job.url,
            goal=payload.goal,
            reasoning=context,
            application_id=application.id,
        )
    except AutomationHalted as exc:
        raise HTTPException(409, str(exc)) from exc
    return {
        **handle.snapshot(),
        "job_id": str(job.id),
        "application_id": str(application.id),
        "document_id": str(document.id) if document else None,
        "resume_path": document.pdf_path if document else None,
    }


async def _resume_document(
    session: AsyncSession, job: m.Job, document_id: UUID | None
) -> m.GeneratedDocument | None:
    """Which CV the agent uploads. Must belong to this job, and must exist on disk."""
    from pathlib import Path

    if document_id is not None:
        document = await session.get(m.GeneratedDocument, document_id)
        if document is None or document.job_id != job.id:
            raise HTTPException(400, "That document was not written for this job.")
        if not document.pdf_path or not Path(document.pdf_path).is_file():
            raise HTTPException(400, "That document has no PDF on disk; regenerate it.")
        return document

    for candidate in await _documents_for(session, job.id):
        if (
            candidate.kind == "tailored_cv"
            and candidate.pdf_path
            and Path(candidate.pdf_path).is_file()
        ):
            return candidate
    return None


@router.post("/{job_id}/unblock")
async def unblock(
    job_id: UUID,
    runs: RunManager = Depends(get_run_manager),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Send a blocked job back to where it was.

    The caller cannot name the target: it comes from the stored `resume_state` only, or
    "unblock" becomes a way to skip the steps in between.
    """
    job, application = await _load(session, job_id)
    P.require_state(application, S.BLOCKED)
    _no_live_run(runs, application)

    target = await P.resume_from_blocked(session, application)
    return {**P.job_view(job, application), "resumed_to": target.value}


@router.post("/{job_id}/cancel")
async def cancel(
    job_id: UUID,
    payload: CancelIn,
    runs: RunManager = Depends(get_run_manager),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Stop caring about this job. Terminal, and refused while a run is still using it."""
    job, application = await _load(session, job_id)
    _no_live_run(runs, application)
    await P.advance(session, application, S.CANCELLED, detail={"reason": payload.reason})
    return P.job_view(job, application)
