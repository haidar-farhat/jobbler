"""Run control: start, pause, resume, stop, and the kill switch."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ...db import models as m
from ...db.session import get_session
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

    job = m.Job(
        url=payload.start_url,
        title=payload.job_title or "Untitled role",
        company=payload.company,
    )
    session.add(job)
    await session.commit()

    application = m.Application(
        job_id=job.id,
        profile_id=profile.id,
        state=ApplicationState.READY_FOR_BROWSER.value,
    )
    session.add(application)
    await session.commit()

    context = await load_reasoning_context(session, payload.goal)
    context.job_title = payload.job_title
    context.company = payload.company

    try:
        handle = await runs.start(
            start_url=payload.start_url,
            goal=payload.goal,
            reasoning=context,
            application_id=application.id,
        )
    except AutomationHalted as exc:
        raise HTTPException(409, str(exc)) from exc

    return handle.snapshot()


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
