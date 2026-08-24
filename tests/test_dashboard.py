"""The zero-build dashboard actually loads and works.

Added after shipping a dashboard whose script had a regex literal broken across two lines.
The whole script failed to parse, so nothing bound, no poll ran, and every status light sat
red -- with the server perfectly healthy behind it. Nothing in the suite noticed, because
every other test talks to the API directly.

A syntax error in one file should not be something the user discovers.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.browser

DASHBOARD = (
    Path(__file__).resolve().parents[1]
    / "services" / "api" / "localapply" / "static" / "dashboard.html"
)


@pytest.fixture
async def page(browser_manager):
    """The dashboard loaded from disk, with console errors captured.

    Served over file:// so this needs no running API. Its fetches fail, which is fine --
    the point is whether the script parses and binds its handlers.
    """
    session = await browser_manager.new_session()
    errors: list[str] = []
    session.page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    session.page.on(
        "console",
        lambda m: errors.append(f"{m.type}: {m.text}") if m.type == "error" else None,
    )
    await session.page.goto(DASHBOARD.as_uri(), wait_until="domcontentloaded")
    await session.page.wait_for_timeout(600)
    return session.page, errors


def _real_errors(errors: list[str]) -> list[str]:
    """Failed fetches are expected over file://; a script error is not.

    The page polls /health and /agent/runs on load, which cannot resolve without a server.
    Those are noise here. A syntax error, a ReferenceError, or a bad regex is the signal.
    """
    ignorable = (
        "failed to fetch",
        "err_connection",
        "net::",
        "favicon",
        "load resource",
        "cannot load",          # "Fetch API cannot load file:///..."
        'url scheme "file"',
    )
    return [e for e in errors if not any(token in e.lower() for token in ignorable)]


async def test_the_script_parses(page):
    """The regression. A syntax error anywhere kills every handler on the page."""
    _, errors = page
    assert _real_errors(errors) == []


async def test_the_controls_exist_and_are_wired(page):
    view, errors = page

    for selector in ("#b-start", "#b-kill", "#b-upload", "#url", "#cv-file"):
        assert await view.query_selector(selector) is not None, f"{selector} missing"

    # A handler is only attached if the script ran to completion.
    assert await view.evaluate("typeof document.getElementById('b-start').onclick") == "function"
    assert await view.evaluate("typeof document.getElementById('b-upload').onclick") == "function"
    assert _real_errors(errors) == []


async def test_switching_to_the_profile_tab_works(page):
    view, _ = page
    await view.click('.tab[data-view="profile"]')
    await view.wait_for_timeout(300)

    assert await view.is_visible("#profile-view")
    assert await view.evaluate(
        "getComputedStyle(document.querySelector('main')).display"
    ) == "none"


async def test_generation_buttons_are_bound(page):
    view, _ = page
    bound = await view.evaluate(
        "Array.from(document.querySelectorAll('[data-gen]'))"
        ".every(b => typeof b.onclick === 'function')"
    )
    assert bound, "the generate buttons never got their handlers"


async def test_the_page_has_no_stray_control_characters():
    """The same class of bug that broke the script: an escaped sequence collapsing into a
    real control character during an edit."""
    text = DASHBOARD.read_text(encoding="utf-8")
    stray = {hex(ord(c)) for c in text if ord(c) < 32 and c not in "\n\t"}
    assert stray == set(), f"control characters in dashboard.html: {stray}"
