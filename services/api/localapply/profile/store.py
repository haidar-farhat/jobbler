"""Reading the knowledge base.

One query, in one place. `status == accepted` is the whole security boundary of the profile:
a proposal is a claim the user has not agreed to, and every caller that forgets the filter
quietly re-opens the hole. It was already copied into two modules, and the job pipeline
would have made three.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ..db import models as m
from .facts import FactCategory, FactStatus


async def accepted_facts(session: AsyncSession, profile_id: UUID) -> list[m.ProfileFact]:
    """Every fact the user has explicitly accepted. Nothing else is usable anywhere."""
    result = await session.execute(
        select(m.ProfileFact).where(
            m.ProfileFact.profile_id == profile_id,
            m.ProfileFact.status == FactStatus.ACCEPTED.value,
        )
    )
    return list(result.scalars().all())


async def accepted_skills(session: AsyncSession, profile_id: UUID) -> set[str]:
    """The skills a job may be scored against.

    A *proposed* skill must never raise a match score: doing so would let an unreviewed CV
    extraction decide which jobs the user is told to apply for.
    """
    facts = await accepted_facts(session, profile_id)
    return {f.value for f in facts if f.category == FactCategory.SKILL.value}
