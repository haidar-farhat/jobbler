"""Run a saved search: read a board, keep what fits, score what is new.

The whole point of the phase, in one sentence: you open the app and there are jobs you never
typed in. Everything below exists to make that true without any of it becoming a way for a
board to decide something on your behalf.

What a run does, and what it deliberately does not:

  * It creates job rows and scores them. It stops at RECOMMENDED, like every other path.
    There is no auto-approve and no score threshold that applies for you -- a posting raises
    its own match score simply by listing skills you have, and the design is safe only
    because nothing automated acts on that number.
  * It filters on the **title** only. Filtering on the description would hand a board's own
    text the decision about what you see, and a posting listing every keyword would always
    win.
  * It dedupes on `(source, external_id)` at the database, not in Python. Running the same
    search twice is a no-op, not a second copy of every job with its own state and no memory
    that you cancelled the first.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ..config import Settings
from ..db import models as m
from ..db.models import utc_now
from ..orchestrator.state_machine import ApplicationState as S
from ..safety import KILL_SWITCH, AutomationHalted
from . import pipeline as P
from .connectors import Posting, connector_for
from .connectors.base import get_json
from .ingest import _pace, check_url

logger = logging.getLogger(__name__)

#: A board with more postings than this is a job board in its own right, not a company
#: careers page. Reading all of them would bury everything else on your board.
MAX_POSTINGS_PER_RUN = 400


@dataclass
class SearchResult:
    """What one run of one search did, in terms a person can act on."""

    fetched: int = 0
    filtered_out: int = 0
    already_known: int = 0
    unusable: int = 0
    added: int = 0
    below_threshold: int = 0
    error: str | None = None
    #: The jobs actually created, best match first.
    jobs: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "fetched": self.fetched,
            "filtered_out": self.filtered_out,
            "already_known": self.already_known,
            "unusable": self.unusable,
            "added": self.added,
            "below_threshold": self.below_threshold,
            "error": self.error,
        }

    @property
    def summary(self) -> str:
        if self.error:
            return self.error
        if not self.fetched:
            return "That board returned no postings."
        return (
            f"{self.fetched} on the board, {self.added} added"
            + (f", {self.already_known} already known" if self.already_known else "")
            + (f", {self.filtered_out} filtered out" if self.filtered_out else "")
            + (f", {self.below_threshold} below your score" if self.below_threshold else "")
        )


def wanted(posting: Posting, include: list[str], exclude: list[str]) -> bool:
    """Does this title pass the search's filters? Title only -- see the module docstring."""
    title = (posting.title or "").casefold()
    if any(term.casefold() in title for term in exclude if term.strip()):
        return False
    terms = [t for t in include if t.strip()]
    return not terms or any(term.casefold() in title for term in terms)


async def fetch_postings(search: m.SavedSearch, settings: Settings) -> list[Posting]:
    """One request to one board, paced and stoppable.

    Raises rather than returning empty on failure: "the board answered with nothing" and
    "we could not reach the board" are different things, and a search that quietly reports
    zero for the second is a search you stop trusting.
    """
    connector = connector_for(search.source)
    if connector is None:
        raise ValueError(f"No connector for {search.source!r}.")
    if KILL_SWITCH.engaged:
        raise AutomationHalted(KILL_SWITCH.reason or "Automation stopped.")

    url = connector.endpoint(search.handle)
    # The same guard the browser ingest uses. A board handle is user-typed, but the endpoint
    # it produces is still a URL leaving this machine, and there is no reason for it to be
    # able to point anywhere but a public host.
    check_url(url, allow_loopback=settings.ingest_allow_loopback)
    host = (urlsplit(url).hostname or "").lower()

    async with httpx.AsyncClient(follow_redirects=True) as client:
        await _pace(host, settings.ingest_min_interval_s)
        try:
            payload = await get_json(client, url)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404 and hasattr(connector, "alternates"):
                # Lever's EU customers are a separate tenant pool, so a 404 on one host is
                # not "no such board" -- it is "not this pool".
                for alternate in connector.alternates(search.handle):
                    await _pace((urlsplit(alternate).hostname or "").lower(),
                                settings.ingest_min_interval_s)
                    try:
                        payload = await get_json(client, alternate)
                    except httpx.HTTPStatusError:
                        continue
                    connector.remember_host(search.handle, alternate)
                    return connector.parse(payload, search.handle)
            raise

    return connector.parse(payload, search.handle)


async def run_search(
    session: AsyncSession,
    search: m.SavedSearch,
    settings: Settings,
    profile: m.Profile,
) -> SearchResult:
    """Fetch, filter, dedupe, create, score. Recording what happened at every step.

    The counts are not decoration. "Nothing new" and "everything was filtered out" and "that
    board is unreachable" look identical from the outside, and a search you cannot tell
    apart is a search you cannot fix.
    """
    result = SearchResult()

    try:
        postings = await fetch_postings(search, settings)
    except AutomationHalted:
        raise
    except Exception as exc:  # noqa: BLE001 - one bad board must not end the whole sweep
        logger.warning("search %s failed: %s", search.id, exc)
        result.error = _readable(exc)
        await _record(session, search, result)
        return result

    result.fetched = len(postings)
    postings = postings[:MAX_POSTINGS_PER_RUN]

    known = await _known_ids(session, search.source)

    for posting in postings:
        if not wanted(posting, search.include or [], search.exclude or []):
            result.filtered_out += 1
            continue
        if posting.external_id in known:
            result.already_known += 1
            continue
        if not posting.usable:
            # A posting with no readable description would score zero and look like a real
            # answer. Counting it separately is what makes a broken field mapping visible
            # rather than silent.
            result.unusable += 1
            continue

        job, application = await P.create_job(
            session,
            profile,
            url=posting.url,
            title=posting.title[:200],
            company=posting.company,
            location=posting.location,
            description=posting.description,
            source=search.source,
            external_id=posting.external_id,
        )
        known.add(posting.external_id)

        await P.advance(session, application, S.PARSED,
                        detail={"via": "discovery", "search_id": str(search.id)})
        _, match = await P.analyze_and_score(
            session, job, application, actor="discovery", source="web"
        )

        if match.score < (search.min_score or 0.0):
            # Kept as a row so it is never fetched again, but taken off the board: a company
            # with 800 roles should not bury the six that matter.
            result.below_threshold += 1
            await P.advance(session, application, S.CANCELLED,
                            detail={"reason": f"below your {search.min_score:.0%} threshold"})
            continue

        result.added += 1
        result.jobs.append(P.job_view(job, application))

    result.jobs.sort(key=lambda j: -(j["match_score"] or 0.0))
    await _record(session, search, result)
    return result


def _readable(exc: Exception) -> str:
    """What went wrong, in words that suggest what to do about it."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code == 404:
            return "No board with that handle. Check the spelling on the board's own URL."
        if code == 429:
            return "That board asked us to slow down. Try again in a few minutes."
        if code in {401, 403}:
            return "That board does not allow reading its postings this way."
        return f"That board answered {code}."
    if isinstance(exc, httpx.TimeoutException):
        return "That board did not answer in time."
    if isinstance(exc, httpx.HTTPError):
        return "Could not reach that board."
    return f"{exc.__class__.__name__}: {exc}"


async def _known_ids(session: AsyncSession, source: str) -> set[str]:
    """Every external id already recorded for this board.

    Read once per run rather than queried per posting: a board of 400 roles would otherwise
    be 400 round trips, and the answer cannot change while we are the only writer.
    """
    rows = await session.execute(
        select(m.Job.external_id).where(m.Job.source == source, m.Job.external_id.isnot(None))
    )
    return {row for (row,) in rows.all() if row}


async def _record(session: AsyncSession, search: m.SavedSearch, result: SearchResult) -> None:
    search.last_run_at = utc_now()
    search.last_result = result.as_dict()
    session.add(search)
    await session.commit()
