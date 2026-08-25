"""Read one job posting the user asked for, through the browser, once.

This is the first code in the tree that fetches a third-party page outside an agent run, so
it is also the first place the project's stated posture has to be built rather than asserted:
**refuse, do not evade.**

  * One URL per ingest, and it is the one the *user typed*. A URL found in page content is
    never followed, which removes attacker-directed fetching as a class rather than as a case.
  * One navigation. No retry, no second session, no second attempt with different headers.
  * No User-Agent, header, or cookie tampering. A plain Chromium context with no stored
    session, so there is no logged-in account to get banned.
  * A login wall or a CAPTCHA stores **nothing** and hands the page to the person. Getting
    past one is their decision to make in their own browser, not something this code tries.
  * Per-host pacing, with the wait held inside the lock so it also serialises ingestion.

What it deliberately does not have: a `robots.txt` parser. There is none in this tree, and
claiming to honour a file nothing reads would be worse than saying plainly that the
mitigations above are what stands in its place.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from dataclasses import dataclass
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from ..browser.executor import BrowserExecutor
from ..browser.observer import Observer
from ..browser.session import BrowserManager
from ..config import Settings
from ..contracts import ActionType, Decision, EventType, Observation, PageKind
from ..db import models as m
from ..db.session import session_factory
from ..events.bus import EventBus
from ..policy.capabilities import capabilities_for
from ..policy.engine import PolicyEngine
from ..policy.rules import RunContext
from ..safety import KILL_SWITCH, AutomationHalted

#: Page kinds that mean "a person has to deal with this", never "try harder".
WALLS: frozenset[PageKind] = frozenset({PageKind.LOGIN, PageKind.CAPTCHA, PageKind.ERROR})

#: Hostnames that resolve to this machine regardless of what the address looks like.
_LOCAL_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost"}


class UnsafeURL(ValueError):
    """A URL this must not open. Refused before a browser is started."""


@dataclass
class IngestBlocked(RuntimeError):
    """The page is a wall. Nothing was stored; a person takes it from here."""

    page_kind: str
    url: str

    def __str__(self) -> str:
        friendly = {
            "login": "That page wants you to sign in first.",
            "captcha": "That page is showing a CAPTCHA.",
            "error": "That page did not load as a job posting.",
        }
        return friendly.get(self.page_kind, "That page could not be read automatically.")


@dataclass
class IngestResult:
    text: str
    page_kind: str
    url: str
    run_id: UUID


def normalise_host(raw: str) -> tuple[str, ipaddress.IPv4Address | ipaddress.IPv6Address | None]:
    """The host as a browser sees it, plus its address if it is a literal one.

    Two normalisations, both learned from a working exfiltration:

      * **A trailing dot is stripped.** `localhost.` is the fully-qualified form of
        `localhost`; a browser resolves it to loopback and preserves the dot in
        `location.href`, so a naive name check misses it both before *and* after a redirect.
      * **Legacy IPv4 forms are parsed.** `ipaddress` accepts only dotted quads, but a
        browser also accepts `2130706433`, `0x7f000001`, `017700000001`, `127.1` and `0` --
        every one of which reaches 127.0.0.1. `socket.inet_aton` accepts exactly that family,
        which is what makes it the right parser here rather than the stricter one.
    """
    host = (raw or "").strip().lower().rstrip(".")
    if not host:
        return "", None

    try:
        return host, ipaddress.ip_address(host)
    except ValueError:
        pass

    # Only strings that are entirely numeric-ish can be a legacy IPv4 literal; a real domain
    # cannot be, because its last label may not be all digits.
    try:
        packed = socket.inet_aton(host)
    except OSError:
        return host, None
    return host, ipaddress.IPv4Address(packed)


def _is_local(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
        or address.is_multicast
    )


def check_url(url: str, *, allow_loopback: bool) -> str:
    """Refuse anything that is not a plain public web page.

    This API has no authentication, so a posting that redirects the browser to
    `http://127.0.0.1:8000/profile` would put the entire accepted-fact set into a job
    description -- from where it flows into a cover-letter prompt and a PDF bound for a
    third party. That is why loopback is refused by default rather than allowed for
    convenience.

    This is the *syntactic* half. A name that resolves to a private address still passes
    here; `resolve_is_public` is what closes that, and the fetch path runs both.
    """
    parts = urlsplit((url or "").strip())
    if parts.scheme not in {"http", "https"}:
        raise UnsafeURL(f"Only http and https URLs can be opened, not {parts.scheme or 'that'!r}.")
    if "@" in (parts.netloc or ""):
        raise UnsafeURL("A URL with credentials in it will not be opened.")

    host, address = normalise_host(parts.hostname or "")
    if not host:
        raise UnsafeURL("That URL has no host.")

    if allow_loopback:
        return url.strip()

    if host in _LOCAL_NAMES or host.endswith(".localhost"):
        raise UnsafeURL("That URL points back at this machine.")
    if address is not None and _is_local(address):
        raise UnsafeURL(f"{host} is a private or local address and will not be opened.")
    return url.strip()


async def resolve_is_public(url: str, *, allow_loopback: bool) -> None:
    """Refuse a *name* that resolves to an address on this side of the network.

    The syntactic check cannot see this: `internal.example.com` looks like any other domain
    until it is resolved. Every address the name resolves to is checked, not just the first,
    because a name with one public and one private answer is the same attack with an extra
    step.

    This narrows DNS rebinding rather than closing it -- the browser resolves the name again
    when it connects, and nothing here can bind that answer to this one. It is written down
    rather than glossed, and it is why the syntactic check stays in place as well.
    """
    if allow_loopback:
        return

    parts = urlsplit((url or "").strip())
    host, literal = normalise_host(parts.hostname or "")
    if not host or literal is not None:
        return  # a literal address was already judged by check_url

    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise UnsafeURL(f"{host} could not be looked up.") from exc

    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if _is_local(address):
            raise UnsafeURL(
                f"{host} resolves to {address}, which is on this machine's own network."
            )


#: Per-host pacing. The sleep is held *inside* the lock, so it also has the effect of
#: serialising ingestion to one page at a time -- which is the behaviour a site sees.
_LAST_FETCH: dict[str, float] = {}
_PACE_LOCK = asyncio.Lock()


async def _pace(host: str, interval: float) -> None:
    async with _PACE_LOCK:
        elapsed = time.monotonic() - _LAST_FETCH.get(host, 0.0)
        if elapsed < interval:
            await asyncio.sleep(interval - elapsed)
        _LAST_FETCH[host] = time.monotonic()


async def fetch_description(
    *,
    browser: BrowserManager,
    bus: EventBus,
    settings: Settings,
    job: m.Job,
    application_id: UUID,
) -> IngestResult:
    """Open the user's URL, read the page text, close the browser.

    Takes a `BrowserManager`, never a `RunManager`: the orchestrator imports the browser
    layer and not the reverse, and inverting that here would be both a layering violation
    and a circular import waiting to happen.
    """
    if KILL_SWITCH.engaged:
        raise AutomationHalted(KILL_SWITCH.reason or "Automation stopped.")

    problem = BrowserManager.check_event_loop()
    if problem:
        raise RuntimeError(problem)

    url = check_url(job.url, allow_loopback=settings.ingest_allow_loopback)
    await resolve_is_public(url, allow_loopback=settings.ingest_allow_loopback)
    host, _ = normalise_host(urlsplit(url).hostname or "")
    await _pace(host, settings.ingest_min_interval_s)

    # Pacing sleeps for seconds, and the switch can be pressed inside that window. Without
    # this the run carried on into `new_session()`, which *relaunches* a browser the switch
    # had just closed -- and the failure then surfaced to the user as "that page did not
    # load", which is not what happened.
    if KILL_SWITCH.engaged:
        raise AutomationHalted(KILL_SWITCH.reason or "Automation stopped.")

    run_id = uuid4()
    await _record_run(run_id, application_id, url)
    await bus.emit(run_id, EventType.RUN_STARTED, f"Reading {url}", agent="discovery")

    try:
        session = await browser.new_session()
    except RuntimeError as exc:
        await bus.emit(run_id, EventType.RUN_FAILED, str(exc), agent="discovery")
        await _finish_run(run_id, "failed", str(exc))
        raise

    try:
        # The loop synthesises exactly this for its own opening navigation: there is no page
        # yet, so there is nothing to have observed.
        seed = Observation(run_id=run_id, url="about:blank", title="")
        context = RunContext(
            agent="discovery",
            capabilities=capabilities_for("discovery"),
            max_actions=1,
            min_confidence=settings.min_decision_confidence,
            dry_run=settings.dry_run,
        )
        decision = Decision(
            action=ActionType.NAVIGATE,
            value=url,
            confidence=1.0,
            reason="Reading the posting the user asked for.",
        )

        # An ingest is structurally incapable of typing, selecting, uploading or submitting:
        # the capability set says so. `granted_approvals` is left empty, so a verdict of
        # REQUIRE_APPROVAL is a hard stop here rather than a prompt.
        verdict = PolicyEngine().evaluate(decision, seed, context)
        if verdict.blocks_execution:
            raise IngestBlocked(page_kind="error", url=url)

        result = await BrowserExecutor(settings).execute(decision, seed, session)
        await _record_action(run_id, session.session_id, decision, result)
        await bus.emit(
            run_id, EventType.ACTION_RESULT,
            f"Opened {url}" if result.success else f"Could not open {url}",
            agent="discovery",
        )
        if not result.success:
            raise IngestBlocked(page_kind="error", url=url)

        observation = await Observer(settings).observe(session, run_id, screenshot=False)

        # The post-redirect check. A posting that 302s somewhere private must be refused
        # *before* a single byte of it is written to the job row. Both halves run again:
        # the redirect target is a URL nobody vetted, and it can be a name as easily as a
        # literal address.
        check_url(observation.url, allow_loopback=settings.ingest_allow_loopback)
        await resolve_is_public(
            observation.url, allow_loopback=settings.ingest_allow_loopback
        )

        if observation.page_kind in WALLS:
            raise IngestBlocked(page_kind=observation.page_kind.value, url=observation.url)

        text = observation.untrusted_text.strip()
        if len(text) < MIN_DESCRIPTION_CHARS:
            raise IngestBlocked(page_kind="error", url=observation.url)

        await bus.emit(run_id, EventType.RUN_FINISHED,
                       f"Read {len(text)} characters.", agent="discovery")
        await _finish_run(run_id, "finished", None)
        return IngestResult(
            text=text, page_kind=observation.page_kind.value, url=observation.url,
            run_id=run_id,
        )
    except IngestBlocked as blocked:
        await bus.emit(run_id, EventType.RUN_FINISHED, str(blocked), agent="discovery")
        await _finish_run(run_id, "finished", str(blocked))
        raise
    except Exception as exc:  # noqa: BLE001 - the run row must record why, whatever happened
        await bus.emit(run_id, EventType.RUN_FAILED, str(exc), agent="discovery")
        await _finish_run(run_id, "failed", f"{exc.__class__.__name__}: {exc}")
        raise
    finally:
        # One session, always released. A capped slot leaked here would eventually make the
        # agent unable to run at all.
        await browser.close_session(session.session_id)


#: Below this a "posting" is a redirect stub, a cookie banner, or a spinner. Refusing it is
#: better than storing it: an empty description scores zero and looks like a real answer.
MIN_DESCRIPTION_CHARS = 200


# --------------------------------------------------------------------------------------
# The durable record. The event bus is an in-memory ring, so these rows are what survives.
# --------------------------------------------------------------------------------------


async def _record_run(run_id: UUID, application_id: UUID, url: str) -> None:
    async with session_factory()() as session:
        session.add(
            m.AgentRun(
                id=run_id,
                application_id=application_id,
                agent="discovery",
                goal="Read this job posting.",
                status="running",
                start_url=url,
            )
        )
        await session.commit()


async def _finish_run(run_id: UUID, status: str, error: str | None) -> None:
    from datetime import UTC, datetime

    async with session_factory()() as session:
        run = await session.get(m.AgentRun, run_id)
        if run is None:
            return
        run.status = status
        run.error = error
        run.finished_at = datetime.now(UTC)
        session.add(run)
        await session.commit()


async def _record_action(run_id: UUID, session_id: UUID, decision, result) -> None:
    async with session_factory()() as session:
        session.add(
            m.BrowserAction(
                run_id=run_id,
                session_id=session_id,
                action=decision.action.value,
                target_ref=decision.target_ref,
                value=decision.value,
                success=result.success,
                error=result.error,
                duration_ms=result.duration_ms,
            )
        )
        await session.commit()


__all__ = [
    "MIN_DESCRIPTION_CHARS",
    "WALLS",
    "IngestBlocked",
    "IngestResult",
    "UnsafeURL",
    "check_url",
    "normalise_host",
    "resolve_is_public",
    "fetch_description",
]
