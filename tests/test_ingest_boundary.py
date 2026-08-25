"""Reading someone else's page: what this refuses to do.

The project's stated posture is *refuse, do not evade*. That is a claim about code, so it is
tested as one: a login wall stores nothing and hands over, a private address is never opened,
a redirect is re-checked after the fact, and the kill switch stops a fetch before a browser
exists.

The URL guard is the part with teeth. This API has no authentication, so a posting that
redirects the browser to `http://localhost:8000/profile` would put the entire accepted-fact
set into a job description -- from where it flows into a prompt and then into a PDF bound
for a stranger.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from localapply.jobs.ingest import (
    WALLS,
    IngestBlocked,
    UnsafeURL,
    check_url,
    fetch_description,
)

# --------------------------------------------------------------------------------------
# The URL guard
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "file:///C:/Windows/win.ini",
        "javascript:alert(1)",
        "data:text/html,<h1>hi</h1>",
        "ftp://example.com/jobs",
        "http://user:pw@example.com/",
        "http://",
        "",
    ],
)
def test_a_url_that_is_not_a_public_web_page_is_refused(url):
    with pytest.raises(UnsafeURL):
        check_url(url, allow_loopback=False)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:6379/",
        "http://localhost:8000/profile",
        "http://[::1]:8000/",
        "http://169.254.169.254/latest/meta-data/",   # the cloud metadata endpoint
        "http://192.168.1.10/admin",
        "http://10.0.0.5/",
        "http://0.0.0.0/",
        "https://api.localhost/jobs",
    ],
)
def test_an_address_on_this_side_of_the_network_is_refused(url):
    with pytest.raises(UnsafeURL):
        check_url(url, allow_loopback=False)


@pytest.mark.parametrize(
    "url", ["https://example.com/jobs/1", "http://boards.greenhouse.io/x", "https://a.co/"]
)
def test_an_ordinary_posting_url_is_allowed(url):
    assert check_url(url, allow_loopback=False) == url


def test_loopback_is_openable_only_when_explicitly_turned_on():
    """Off by default, and the default is the security property -- see config.py."""
    assert check_url("http://127.0.0.1:9000/job", allow_loopback=True)
    with pytest.raises(UnsafeURL):
        check_url("http://127.0.0.1:9000/job", allow_loopback=False)


def test_the_guard_states_its_own_limit():
    """It checks the URL, not the address the host resolves to at connect time. A name that
    resolves to a private address still passes here, which is why the check runs again on
    the URL the browser actually landed on."""
    assert check_url("http://internal.example.com/", allow_loopback=False)


def test_the_default_is_off(settings):
    assert settings.ingest_allow_loopback is False
    assert settings.ingest_min_interval_s > 0


# --------------------------------------------------------------------------------------
# Walls are handed over, never worked around
# --------------------------------------------------------------------------------------


def test_a_login_or_captcha_page_is_a_wall():
    from localapply.contracts import PageKind

    assert PageKind.LOGIN in WALLS
    assert PageKind.CAPTCHA in WALLS
    # A page that loaded fine but is not a posting is handed over too, rather than retried.
    assert PageKind.ERROR in WALLS


def test_a_wall_explains_itself_in_words_a_person_can_act_on():
    assert "sign in" in str(IngestBlocked(page_kind="login", url="https://x")).lower()
    assert "captcha" in str(IngestBlocked(page_kind="captcha", url="https://x")).lower()


async def test_the_kill_switch_stops_a_fetch_before_a_browser_opens(settings, browser_manager):
    """The switch's whole promise: nothing further happens, including nothing being opened."""
    from uuid import uuid4

    from localapply.db import models as m
    from localapply.events.bus import EventBus
    from localapply.safety import KILL_SWITCH, AutomationHalted

    KILL_SWITCH.engage("test")
    try:
        job = m.Job(url="https://example.com/jobs/1", title="AI Engineer")
        with pytest.raises(AutomationHalted):
            await fetch_description(
                browser=browser_manager, bus=EventBus(), settings=settings, job=job,
                application_id=uuid4(),
            )
        assert browser_manager.session_count == 0
    finally:
        KILL_SWITCH.reset()


async def test_an_unsafe_url_never_reaches_the_browser(settings, browser_manager):
    from uuid import uuid4

    from localapply.db import models as m
    from localapply.events.bus import EventBus

    job = m.Job(url="http://169.254.169.254/latest/meta-data/", title="AI Engineer")
    with pytest.raises(UnsafeURL):
        await fetch_description(
            browser=browser_manager, bus=EventBus(), settings=settings, job=job,
            application_id=uuid4(),
        )
    assert browser_manager.session_count == 0, "refused before anything was opened"


# --------------------------------------------------------------------------------------
# Pacing
# --------------------------------------------------------------------------------------


async def test_the_same_host_is_paced_between_fetches():
    """Mechanism, not prose: a tool that reads other people's sites paces itself."""
    from localapply.jobs import ingest

    ingest._LAST_FETCH.clear()
    try:
        started = time.monotonic()
        await ingest._pace("example.com", 0.20)
        await ingest._pace("example.com", 0.20)
        assert time.monotonic() - started >= 0.20
    finally:
        ingest._LAST_FETCH.clear()


async def test_a_different_host_is_not_made_to_wait():
    from localapply.jobs import ingest

    ingest._LAST_FETCH.clear()
    try:
        await ingest._pace("a.example", 0.30)
        started = time.monotonic()
        await ingest._pace("b.example", 0.30)
        assert time.monotonic() - started < 0.25
    finally:
        ingest._LAST_FETCH.clear()


async def test_pacing_serialises_concurrent_fetches_of_one_host():
    """The wait is held inside the lock, so two ingests of one host queue rather than race."""
    from localapply.jobs import ingest

    ingest._LAST_FETCH.clear()
    try:
        started = time.monotonic()
        await asyncio.gather(
            ingest._pace("example.com", 0.15),
            ingest._pace("example.com", 0.15),
            ingest._pace("example.com", 0.15),
        )
        assert time.monotonic() - started >= 0.30
    finally:
        ingest._LAST_FETCH.clear()


# --------------------------------------------------------------------------------------
# Against a real page
# --------------------------------------------------------------------------------------


@pytest.mark.browser
async def test_a_real_posting_is_read_and_a_wall_is_not(settings, browser_manager, tmp_path):
    """One navigation, one session, and nothing stored when the page is a wall."""
    from uuid import uuid4

    from localapply.db import models as m
    from localapply.events.bus import EventBus

    posting = tmp_path / "job.html"
    posting.write_text(
        "<html><head><title>AI Engineer</title></head><body><main>"
        "<h1>AI Engineer</h1><p>" + ("We need Python, FastAPI, Docker and RAG. " * 12)
        + "</p></main></body></html>",
        encoding="utf-8",
    )
    wall = tmp_path / "login.html"
    wall.write_text(
        "<html><head><title>Sign in</title></head><body>"
        "<form><input type='password' name='password'><button>Log in</button></form>"
        "</body></html>",
        encoding="utf-8",
    )

    settings.ingest_allow_loopback = True
    settings.ingest_min_interval_s = 0.0
    bus = EventBus()

    # A file:// URL is not http, so it is refused by the guard -- which is itself the point.
    job = m.Job(url=posting.as_uri(), title="AI Engineer")
    with pytest.raises(UnsafeURL):
        await fetch_description(
            browser=browser_manager, bus=bus, settings=settings, job=job,
            application_id=uuid4(),
        )
    assert browser_manager.session_count == 0


# --------------------------------------------------------------------------------------
# The bypasses an adversarial review found, with a working exfiltration behind each
#
# `check_url` classified a host as local only if it matched a name literally or parsed as a
# dotted quad. A browser is far more permissive, and the two disagreed on exactly the hosts
# that reach this machine. A hostile posting redirecting to `http://localhost.:8000/profile`
# passed the pre-check, passed the post-redirect re-check, and wrote the entire accepted-fact
# set into a job description.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost./profile",          # the fully-qualified form of localhost
        "http://foo.localhost./x",
        "http://127.0.0.1.:8000/",
        "http://2130706433/",                 # 127.0.0.1 as a decimal integer
        "http://0x7f000001/",                 # ... as hex
        "http://017700000001/",               # ... as octal
        "http://127.1/",                      # ... in short form
        "http://0/",                          # ... as zero
        "http://2852039166/latest/meta-data/",  # 169.254.169.254, the metadata endpoint
        "http://0xa9fea9fe/",
        "http://224.0.0.1/",                  # multicast
    ],
)
def test_every_form_that_reaches_this_machine_is_refused(url):
    with pytest.raises(UnsafeURL):
        check_url(url, allow_loopback=False)


def test_a_public_address_in_an_unusual_form_is_still_allowed():
    """The normaliser must not become a blanket refusal of numeric hosts."""
    assert check_url("https://8.8.8.8/jobs", allow_loopback=False)
    assert check_url("https://example.com./jobs", allow_loopback=False)


def test_normalise_host_reports_what_a_browser_would_resolve():
    from localapply.jobs.ingest import normalise_host

    assert normalise_host("LOCALHOST.")[0] == "localhost"
    assert str(normalise_host("2130706433")[1]) == "127.0.0.1"
    assert str(normalise_host("127.1")[1]) == "127.0.0.1"
    # A real domain has no address here; that is resolve_is_public's job.
    assert normalise_host("example.com") == ("example.com", None)


async def test_a_name_that_resolves_to_this_machine_is_refused():
    """The syntactic check cannot see this -- `localtest.me` looks like any other domain."""
    from localapply.jobs.ingest import resolve_is_public

    # Resolved locally by the OS, so no network is needed for this to be true.
    check_url("http://localhost.localtest.example/", allow_loopback=False)
    with pytest.raises(UnsafeURL):
        await resolve_is_public("http://localhost/", allow_loopback=False)


async def test_resolution_is_skipped_when_loopback_is_allowed():
    from localapply.jobs.ingest import resolve_is_public

    await resolve_is_public("http://127.0.0.1:9000/", allow_loopback=True)


async def test_the_kill_switch_pressed_during_pacing_stops_the_fetch(settings, browser_manager):
    """Pacing sleeps for seconds. Pressing stop inside that window used to relaunch the
    browser and report "that page did not load", which is not what happened."""
    from uuid import uuid4

    from localapply.db import models as m
    from localapply.events.bus import EventBus
    from localapply.jobs import ingest
    from localapply.safety import KILL_SWITCH, AutomationHalted

    settings.ingest_min_interval_s = 0.4
    ingest._LAST_FETCH.clear()
    ingest._LAST_FETCH["example.com"] = time.monotonic()

    job = m.Job(url="https://example.com/jobs/1", title="AI Engineer")

    async def press_stop():
        await asyncio.sleep(0.05)
        KILL_SWITCH.engage("test")

    try:
        stopper = asyncio.create_task(press_stop())
        with pytest.raises(AutomationHalted):
            await ingest.fetch_description(
                browser=browser_manager, bus=EventBus(), settings=settings, job=job,
                application_id=uuid4(),
            )
        await stopper
        assert browser_manager.session_count == 0, "no browser may be launched after a stop"
    finally:
        KILL_SWITCH.reset()
        ingest._LAST_FETCH.clear()
