"""Run control: start, pause, resume, stop, and the kill switch."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ...db import models as m
from ...db.session import get_session
from ...jobs import pipeline as P
from ...orchestrator.run_loop import RunManager
from ...orchestrator.state_machine import ApplicationState
from ...safety import KILL_SWITCH, AutomationHalted
from ..deps import get_run_manager
from .profile import current_profile, load_reasoning_context

router = APIRouter(prefix="/agent", tags=["agent"])


class StartRunIn(BaseModel):
    start_url: str
    goal: str = "Complete this job application."
    job_title: str | None = None
    company: str | None = None
    #: Pasted job description. Drives requirement matching and CV tailoring.
    description: str = ""
    #: Generate a tailored CV for this job and upload that instead of whatever
    #: `resume_path` currently points at.
    tailor_cv: bool = True


@router.post("/runs", status_code=201)
async def start_run(
    payload: StartRunIn,
    runs: RunManager = Depends(get_run_manager),
    session: AsyncSession = Depends(get_session),
) -> dict:
    if KILL_SWITCH.engaged:
        raise HTTPException(
            409,
            f"Automation is stopped ({KILL_SWITCH.reason}). "
            "Re-arm with POST /agent/kill-switch/reset before starting a run.",
        )

    profile = await current_profile(session)
    if profile is None:
        raise HTTPException(400, "Create a profile before running the agent.")

    # The same pipeline the jobs board walks, rather than a second path that jumped
    # straight to READY_FOR_BROWSER. That shortcut was the reason seven states existed only
    # as enum members, and the reason `applications.state` had two writers.
    job, application = await P.create_job(
        session,
        profile,
        url=payload.start_url,
        title=payload.job_title or "Untitled role",
        company=payload.company,
        location=None,
        description=payload.description,
        source="fixture",
        external_id=None,
    )
    await P.advance(session, application, ApplicationState.PARSED,
                    detail={"via": "start_run"})
    # Deliberately unconditional. The dashboard's Start button sends no description, and an
    # empty one scores zero with a "skip" recommendation rather than raising -- so gating
    # this on there being text is what would make the app's oldest entry point illegal.
    await P.analyze_and_score(session, job, application, source="web")
    # Starting a run *is* the human act of approving it; there is no separate gate on this
    # path, and the audit row says which path it came from.
    await P.advance(session, application, ApplicationState.USER_APPROVED,
                    detail={"via": "start_run"})

    context = await load_reasoning_context(session, payload.goal)
    context.job_title = payload.job_title
    context.company = payload.company

    await P.advance(session, application, ApplicationState.DOCUMENTS_GENERATING)
    tailored: dict | None = None
    if payload.tailor_cv and payload.job_title:
        # Generate a CV for this specific job and point the upload at it, so the agent
        # attaches a document tailored to the posting rather than a stale generic file.
        # A failure here must not block the run: it falls back to the existing resume_path
        # and says so in the response.
        tailored = await _tailored_cv(session, runs.settings, profile, job, payload)
        if tailored and tailored.get("pdf_path"):
            context.profile["resume_path"] = tailored["pdf_path"]
    # Written even when tailoring was skipped or failed, preserving the "applied with your
    # existing CV" contract -- and keeping READY_FOR_BROWSER reachable only through here.
    await P.advance(session, application, ApplicationState.READY_FOR_BROWSER,
                    detail={"tailored": bool(tailored and tailored.get("pdf_path"))})

    try:
        handle = await runs.start(
            start_url=payload.start_url,
            goal=payload.goal,
            reasoning=context,
            application_id=application.id,
        )
    except AutomationHalted as exc:
        raise HTTPException(409, str(exc)) from exc

    return {
        **handle.snapshot(),
        "tailored_cv": tailored,
        "job_id": str(job.id),
        "application_id": str(application.id),
    }


async def _tailored_cv(session, settings, profile, job, payload) -> dict:
    """Build and store a tailored CV for this job.

    Kept off the run's critical path on purpose: a document problem should degrade to
    "applied with your existing CV", not "could not apply". Every failure path returns an
    explanation rather than raising.

    The work itself is `documents.pipeline.build_document`, shared with `POST /generate` and
    the jobs board. This used to be a third copy, and it had drifted: it hardcoded
    `version=1`, so a second run for the same job reused the version number, and it never
    set `pdf_sha256`, so a CV the app had generated was not recognised as its own output
    when it came back in through the importer.
    """
    from ...documents.pipeline import NoAcceptedFacts, UngroundedDocument, build_document

    try:
        result = await build_document(
            session,
            settings,
            profile,
            kind="tailored_cv",
            job_title=payload.job_title,
            company=payload.company,
            description=payload.description,
            job_id=job.id,
        )
    except NoAcceptedFacts:
        return {"error": "No accepted profile facts, so no CV was generated."}
    except (UngroundedDocument, ValueError) as exc:
        return {"error": str(exc)}

    document = result.document
    if result.pdf_error:
        return {
            "id": str(document.id),
            "match_score": document.match_score,
            "error": f"PDF rendering failed ({result.pdf_error}); "
                     "the run will upload your existing CV.",
        }

    return {
        "id": str(document.id),
        "version": document.version,
        "match_score": document.match_score,
        "match": document.match_breakdown,
        "pdf_path": document.pdf_path,
    }


@router.get("/runs")
async def list_runs(runs: RunManager = Depends(get_run_manager)) -> list[dict]:
    return [h.snapshot() for h in runs.runs.values()]


@router.get("/runs/{run_id}")
async def read_run(run_id: UUID, runs: RunManager = Depends(get_run_manager)) -> dict:
    handle = runs.get(run_id)
    if handle is None:
        raise HTTPException(404, "No such run.")
    return handle.snapshot()


@router.get("/runs/{run_id}/actions")
async def run_actions(run_id: UUID, session: AsyncSession = Depends(get_session)) -> list[dict]:
    """Everything the executor actually did. An empty list is proof nothing happened."""
    result = await session.execute(
        select(m.BrowserAction).where(m.BrowserAction.run_id == run_id).order_by(m.BrowserAction.ts)
    )
    return [a.model_dump(mode="json") for a in result.scalars().all()]


def _handle_or_404(runs: RunManager, run_id: UUID):
    handle = runs.get(run_id)
    if handle is None:
        raise HTTPException(404, "No such run.")
    return handle


@router.post("/runs/{run_id}/pause")
async def pause_run(run_id: UUID, runs: RunManager = Depends(get_run_manager)) -> dict:
    _handle_or_404(runs, run_id)
    await runs.pause(run_id)
    return runs.get(run_id).snapshot()


@router.post("/runs/{run_id}/resume")
async def resume_run(run_id: UUID, runs: RunManager = Depends(get_run_manager)) -> dict:
    _handle_or_404(runs, run_id)
    await runs.resume(run_id)
    return runs.get(run_id).snapshot()


@router.post("/runs/{run_id}/stop")
async def stop_run(run_id: UUID, runs: RunManager = Depends(get_run_manager)) -> dict:
    _handle_or_404(runs, run_id)
    await runs.stop(run_id)
    return runs.get(run_id).snapshot()


# --------------------------------------------------------------------------------------
# Kill switch
# --------------------------------------------------------------------------------------


class KillSwitchIn(BaseModel):
    reason: str = "Stopped by user"


@router.post("/kill-switch")
async def stop_all_automation(
    payload: KillSwitchIn, runs: RunManager = Depends(get_run_manager)
) -> dict:
    """STOP ALL AUTOMATION.

    Engages the global kill switch, cancels every run, and closes every browser session.
    Re-arming is a separate, explicit call -- stopping must never be undone by accident.
    """
    await runs.stop_all(payload.reason)
    return {"kill_switch": KILL_SWITCH.status(), "runs_stopped": len(runs.runs)}


@router.post("/kill-switch/reset")
async def reset_kill_switch() -> dict:
    KILL_SWITCH.reset()
    return {"kill_switch": KILL_SWITCH.status()}


@router.get("/kill-switch")
async def read_kill_switch() -> dict:
    return KILL_SWITCH.status()
