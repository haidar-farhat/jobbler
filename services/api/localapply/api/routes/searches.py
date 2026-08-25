"""Saved searches: boards to watch, and running them.

A search is a board plus a filter. Running one reads that board's public JSON API once,
creates a job for every posting that is new and passes the filter, and scores each against
your accepted skills -- stopping at RECOMMENDED like every other path into the pipeline.

Nothing here runs itself. See `SavedSearch` for why a local-first app that is only on when
you are looking at it is the wrong place to hide a timer.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ...config import Settings
from ...db import models as m
from ...db.session import get_session
from ...jobs.connectors import BOARDS
from ...jobs.discovery import run_search
from ...safety import KILL_SWITCH, AutomationHalted
from ..deps import get_app_settings
from .profile import current_profile

router = APIRouter(prefix="/searches", tags=["discovery"])

#: A filter list longer than this is a search that should be two searches.
MAX_TERMS = 20


class SearchIn(BaseModel):
    source: str
    handle: str
    label: str = ""
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    min_score: float = 0.0
    enabled: bool = True


class SearchEdit(BaseModel):
    label: str | None = None
    include: list[str] | None = None
    exclude: list[str] | None = None
    min_score: float | None = None
    enabled: bool | None = None


def _view(search: m.SavedSearch) -> dict:
    return {
        "id": str(search.id),
        "source": search.source,
        "handle": search.handle,
        "label": search.label or f"{search.source}/{search.handle}",
        "include": search.include or [],
        "exclude": search.exclude or [],
        "min_score": search.min_score,
        "enabled": search.enabled,
        "last_run_at": search.last_run_at.isoformat() if search.last_run_at else None,
        "last_result": search.last_result or {},
    }


def _clean(terms: list[str] | None) -> list[str]:
    return [" ".join(t.split()) for t in (terms or []) if t.strip()][:MAX_TERMS]


async def _load(session: AsyncSession, search_id: UUID) -> m.SavedSearch:
    search = await session.get(m.SavedSearch, search_id)
    if search is None:
        raise HTTPException(404, "No such search.")
    return search


@router.get("/boards")
async def list_boards() -> list[dict]:
    """Which boards can be read, and what each one calls its handle.

    Every board names it something different -- board token, company slug, job board name --
    and asking for "the handle" without saying which is how you get a 404 you cannot debug.
    """
    return [
        {
            "source": connector.source,
            "handle_label": connector.handle_label,
            "example": connector.endpoint("acme"),
        }
        for connector in BOARDS.values()
    ]


@router.post("", status_code=201)
async def add_search(payload: SearchIn, session: AsyncSession = Depends(get_session)) -> dict:
    profile = await current_profile(session)
    if profile is None:
        raise HTTPException(400, "Create a profile before adding a search.")
    if payload.source not in BOARDS:
        raise HTTPException(
            400, f"Unknown board {payload.source!r}. Known: {', '.join(sorted(BOARDS))}."
        )
    handle = payload.handle.strip().strip("/")
    if not handle:
        raise HTTPException(400, "That search needs a board handle.")

    existing = (
        await session.execute(
            select(m.SavedSearch).where(
                m.SavedSearch.source == payload.source, m.SavedSearch.handle == handle
            )
        )
    ).scalars().first()
    if existing is not None:
        raise HTTPException(409, f"You are already watching {payload.source}/{handle}.")

    search = m.SavedSearch(
        profile_id=profile.id,
        source=payload.source,
        handle=handle,
        label=payload.label.strip(),
        include=_clean(payload.include),
        exclude=_clean(payload.exclude),
        min_score=max(0.0, min(1.0, payload.min_score)),
        enabled=payload.enabled,
    )
    session.add(search)
    await session.commit()
    return _view(search)


@router.get("")
async def list_searches(session: AsyncSession = Depends(get_session)) -> list[dict]:
    rows = await session.execute(
        select(m.SavedSearch).order_by(m.SavedSearch.created_at)
    )
    return [_view(s) for s in rows.scalars().all()]


@router.patch("/{search_id}")
async def edit_search(
    search_id: UUID, payload: SearchEdit, session: AsyncSession = Depends(get_session)
) -> dict:
    search = await _load(session, search_id)
    if payload.label is not None:
        search.label = payload.label.strip()
    if payload.include is not None:
        search.include = _clean(payload.include)
    if payload.exclude is not None:
        search.exclude = _clean(payload.exclude)
    if payload.min_score is not None:
        search.min_score = max(0.0, min(1.0, payload.min_score))
    if payload.enabled is not None:
        search.enabled = payload.enabled
    session.add(search)
    await session.commit()
    return _view(search)


@router.delete("/{search_id}", status_code=204)
async def delete_search(
    search_id: UUID, session: AsyncSession = Depends(get_session)
) -> None:
    """Stop watching a board. The jobs it already found stay -- they are yours now."""
    search = await session.get(m.SavedSearch, search_id)
    if search is not None:
        await session.delete(search)
        await session.commit()


@router.post("/{search_id}/run")
async def run_one(
    search_id: UUID,
    settings: Settings = Depends(get_app_settings),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Read the board now, and put what is new on your job board."""
    if KILL_SWITCH.engaged:
        raise HTTPException(
            409,
            f"Automation is stopped ({KILL_SWITCH.reason}). "
            "Re-arm with POST /agent/kill-switch/reset before running a search.",
        )
    search = await _load(session, search_id)
    profile = await current_profile(session)
    if profile is None:
        raise HTTPException(400, "Create a profile first.")

    try:
        result = await run_search(session, search, settings, profile)
    except AutomationHalted as exc:
        raise HTTPException(409, str(exc)) from exc

    return {
        "search": _view(search),
        "summary": result.summary,
        **result.as_dict(),
        "jobs": result.jobs,
    }


@router.post("/run")
async def run_all(
    settings: Settings = Depends(get_app_settings),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Read every enabled board, one at a time.

    Sequential on purpose. Fetches are paced per host anyway, and a sweep that hammers four
    boards at once to save twelve seconds is the sort of thing this project has said it
    would not do.
    """
    if KILL_SWITCH.engaged:
        raise HTTPException(409, f"Automation is stopped ({KILL_SWITCH.reason}).")
    profile = await current_profile(session)
    if profile is None:
        raise HTTPException(400, "Create a profile first.")

    rows = await session.execute(
        select(m.SavedSearch).where(m.SavedSearch.enabled == True)  # noqa: E712 - SQL, not Python
        .order_by(m.SavedSearch.created_at)
    )
    searches = list(rows.scalars().all())

    ran: list[dict] = []
    added = 0
    for search in searches:
        try:
            result = await run_search(session, search, settings, profile)
        except AutomationHalted as exc:
            # The switch was pressed mid-sweep. Stop where we are and say so, rather than
            # carrying on through the remaining boards.
            ran.append({"search": _view(search), "summary": str(exc), "error": str(exc)})
            break
        added += result.added
        ran.append({"search": _view(search), "summary": result.summary, **result.as_dict()})

    return {
        "searches_run": len(ran),
        "added": added,
        "results": ran,
        "summary": (
            f"{added} new job{'s' if added != 1 else ''} from {len(ran)} board"
            f"{'s' if len(ran) != 1 else ''}."
            if ran
            else "No searches are enabled."
        ),
    }
