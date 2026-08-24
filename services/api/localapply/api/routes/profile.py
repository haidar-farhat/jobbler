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
from ...db.session import get_session

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
    """Build the reasoner's view of the user from verified facts only.

    Facts in the `answer` category become *drafts* for REVIEW_REQUIRED fields: proposals the
    human confirms, never values entered blind.
    """
    profile = await current_profile(session)
    if profile is None:
        return ReasoningContext(goal=goal)

    result = await session.execute(
        select(m.ProfileFact).where(
            m.ProfileFact.profile_id == profile.id,
            m.ProfileFact.verified == True,  # noqa: E712 - SQL comparison, not a Python bool
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


@router.post("/facts/{fact_id}/verify")
async def verify_fact(fact_id: UUID, session: AsyncSession = Depends(get_session)) -> dict:
    fact = await session.get(m.ProfileFact, fact_id)
    if fact is None:
        raise HTTPException(404, "No such fact.")
    fact.verified = True
    session.add(fact)
    await session.commit()
    return fact.model_dump(mode="json")


@router.delete("/facts/{fact_id}", status_code=204)
async def delete_fact(fact_id: UUID, session: AsyncSession = Depends(get_session)) -> None:
    fact = await session.get(m.ProfileFact, fact_id)
    if fact is not None:
        await session.delete(fact)
        await session.commit()
