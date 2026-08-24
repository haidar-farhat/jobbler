"""The professional knowledge base.

Facts are individually approved. Nothing reaches an application unless its status is
`accepted`, so a CV import can *propose* facts without ever silently rewriting your identity.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ...ai.reasoner import ReasoningContext
from ...db import models as m
from ...db.models import utc_now
from ...db.session import get_session
from ...profile.facts import USABLE_STATUSES, FactStatus

router = APIRouter(prefix="/profile", tags=["profile"])


class ProfileIn(BaseModel):
    full_name: str
    email: str


class FactIn(BaseModel):
    key: str
    value: str
    category: str = "identity"
    source: str = "manual"
    confidence: float = 1.0
    #: Facts added by hand are accepted by definition -- you just typed them. Imported ones
    #: come in as "proposed" and must be accepted individually.
    status: str = FactStatus.ACCEPTED.value


async def current_profile(session: AsyncSession) -> m.Profile | None:
    result = await session.execute(select(m.Profile).limit(1))
    return result.scalars().first()


async def load_reasoning_context(session: AsyncSession, goal: str) -> ReasoningContext:
    """Build the reasoner's view of the user from accepted facts only.

    This is the enforcement point for the whole knowledge base: a proposed, rejected, or
    superseded fact is invisible here, so it can never be typed into an application.

    Facts in the `answer` category become *drafts* for REVIEW_REQUIRED fields: proposals the
    human confirms, never values entered blind.
    """
    profile = await current_profile(session)
    if profile is None:
        return ReasoningContext(goal=goal)

    result = await session.execute(
        select(m.ProfileFact).where(
            m.ProfileFact.profile_id == profile.id,
            m.ProfileFact.status.in_(USABLE_STATUSES),
        )
    )
    facts = list(result.scalars().all())
    return ReasoningContext(
        goal=goal,
        profile={f.key: f.value for f in facts if f.category != "answer"},
        drafts={f.key: f.value for f in facts if f.category == "answer"},
    )


@router.get("")
async def read_profile(session: AsyncSession = Depends(get_session)) -> dict:
    profile = await current_profile(session)
    if profile is None:
        return {"profile": None, "facts": []}
    result = await session.execute(
        select(m.ProfileFact).where(m.ProfileFact.profile_id == profile.id)
    )
    return {
        "profile": profile.model_dump(mode="json"),
        "facts": [f.model_dump(mode="json") for f in result.scalars().all()],
    }


@router.post("", status_code=201)
async def create_profile(
    payload: ProfileIn, session: AsyncSession = Depends(get_session)
) -> dict:
    existing = await current_profile(session)
    if existing is not None:
        raise HTTPException(409, "A profile already exists.")
    profile = m.Profile(full_name=payload.full_name, email=payload.email)
    session.add(profile)
    await session.commit()
    return profile.model_dump(mode="json")


@router.post("/facts", status_code=201)
async def add_fact(payload: FactIn, session: AsyncSession = Depends(get_session)) -> dict:
    profile = await current_profile(session)
    if profile is None:
        raise HTTPException(404, "Create a profile first.")
    fact = m.ProfileFact(profile_id=profile.id, **payload.model_dump())
    session.add(fact)
    await session.commit()
    return fact.model_dump(mode="json")


@router.post("/facts/{fact_id}/accept")
async def accept_fact(fact_id: UUID, session: AsyncSession = Depends(get_session)) -> dict:
    """Accept a fact, superseding whatever it replaces.

    Superseding happens here rather than at import time so the old value survives until you
    actually say yes -- declining a proposal leaves the profile exactly as it was.
    """
    fact = await session.get(m.ProfileFact, fact_id)
    if fact is None:
        raise HTTPException(404, "No such fact.")

    if fact.supersedes_id is not None:
        previous = await session.get(m.ProfileFact, fact.supersedes_id)
        if previous is not None and previous.status == FactStatus.ACCEPTED.value:
            previous.status = FactStatus.SUPERSEDED.value
            previous.resolved_at = utc_now()
            session.add(previous)

    fact.status = FactStatus.ACCEPTED.value
    fact.resolved_at = utc_now()
    session.add(fact)
    await session.commit()
    return fact.model_dump(mode="json")


@router.post("/facts/{fact_id}/reject")
async def reject_fact(fact_id: UUID, session: AsyncSession = Depends(get_session)) -> dict:
    """Decline a proposal. Remembered, so the same value is not proposed at you again."""
    fact = await session.get(m.ProfileFact, fact_id)
    if fact is None:
        raise HTTPException(404, "No such fact.")
    fact.status = FactStatus.REJECTED.value
    fact.resolved_at = utc_now()
    session.add(fact)
    await session.commit()
    return fact.model_dump(mode="json")


@router.delete("/facts/{fact_id}", status_code=204)
async def delete_fact(fact_id: UUID, session: AsyncSession = Depends(get_session)) -> None:
    fact = await session.get(m.ProfileFact, fact_id)
    if fact is not None:
        await session.delete(fact)
        await session.commit()
