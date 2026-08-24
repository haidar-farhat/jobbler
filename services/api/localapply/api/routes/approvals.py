"""Human-in-the-loop approvals.

Resolving an approval is the only way a REQUIRE_APPROVAL decision ever executes. Every
resolution is written to `audit_logs` with actor and source, so "who approved this and from
where" is always answerable.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ...db import models as m
from ...db.session import get_session
from ...orchestrator.run_loop import RunManager
from ..deps import get_run_manager

router = APIRouter(prefix="/approvals", tags=["approvals"])


class ResolveIn(BaseModel):
    approved: bool
    #: Optional correction. An edited value produces a new fingerprint, so the approval
    #: authorises exactly the action the user saw and changed -- not the original proposal.
    edited_value: str | None = None
    actor: str = "user"
    #: "web" or "phone". Recorded in the audit log.
    source: str = "web"


@router.get("")
async def list_approvals(
    status: str = "pending", session: AsyncSession = Depends(get_session)
) -> list[dict]:
    result = await session.execute(
        select(m.Approval).where(m.Approval.status == status).order_by(m.Approval.created_at)
    )
    return [a.model_dump(mode="json") for a in result.scalars().all()]


@router.get("/{approval_id}")
async def read_approval(
    approval_id: UUID, session: AsyncSession = Depends(get_session)
) -> dict:
    approval = await session.get(m.Approval, approval_id)
    if approval is None:
        raise HTTPException(404, "No such approval.")
    return approval.model_dump(mode="json")


@router.post("/{approval_id}/resolve")
async def resolve(
    approval_id: UUID,
    payload: ResolveIn,
    runs: RunManager = Depends(get_run_manager),
    session: AsyncSession = Depends(get_session),
) -> dict:
    approval = await session.get(m.Approval, approval_id)
    if approval is None:
        raise HTTPException(404, "No such approval.")
    if approval.status != "pending":
        raise HTTPException(409, f"Already {approval.status}.")

    try:
        await runs.resolve_approval(
            approval.run_id,
            approval_id,
            approved=payload.approved,
            edited_value=payload.edited_value,
            actor=payload.actor,
            source=payload.source,
        )
    except KeyError as exc:
        raise HTTPException(409, str(exc)) from exc

    await session.refresh(approval)
    return approval.model_dump(mode="json")
