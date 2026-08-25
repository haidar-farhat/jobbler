"""The zero-build dashboard actually loads and works.

Added after shipping a dashboard whose script had a regex literal broken across two lines.
The whole script failed to parse, so nothing bound, no poll ran, and every status light sat
red -- with the server perfectly healthy behind it. Nothing in the suite noticed, because
every other test talks to the API directly.

A syntax error in one file should not be something the user discovers.
"""

from __future__ import annotations

import json
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


SAMPLE_FACTS = """[
  {"id": "11111111-1111-1111-1111-111111111111", "category": "experience",
   "status": "proposed", "source": "cv_import", "value": "Dev, Carepool",
   "detail": {"role": "Full-Stack Developer", "organisation": "Carepool",
              "dates": "", "bullets": ["Built the API", "Cut latency"]}},
  {"id": "22222222-2222-2222-2222-222222222222", "category": "skill",
   "status": "accepted", "source": "cv_import", "value": "Python", "detail": {}},
  {"id": "33333333-3333-3333-3333-333333333333", "category": "experience",
   "status": "rejected", "source": "cv_import", "value": "Wrong", "detail": {}}
]"""


async def test_the_entry_editor_renders_editable_fields(page):
    """The parser cannot read every CV. This is where a wrong date gets fixed, so the
    fields have to actually be there and hold the parsed values."""
    view, errors = page
    await view.evaluate(f"renderEntries({SAMPLE_FACTS})")

    assert await view.query_selector('#entries [data-entry]') is not None
    role = await view.eval_on_selector(
        '#entries input[data-k="role"]', "el => el.value"
    )
    assert role == "Full-Stack Developer"

    # A CV with no readable date range leaves the field empty rather than dropping it --
    # an empty box is an invitation to type; a missing one is a dead end.
    assert await view.query_selector('#entries input[data-k="dates"]') is not None

    bullets = await view.eval_on_selector(
        '#entries textarea[data-k="bullets"]', "el => el.value"
    )
    assert bullets == "Built the API\nCut latency"
    assert _real_errors(errors) == []


async def test_the_entry_editor_shows_only_entries_still_in_play(page):
    view, _ = page
    await view.evaluate(f"renderEntries({SAMPLE_FACTS})")

    cards = await view.query_selector_all("#entries [data-entry]")
    assert len(cards) == 1, "skills and rejected entries do not belong in the entry editor"

    body = await view.eval_on_selector("#entries", "el => el.textContent")
    assert "Wrong" not in body


async def test_a_proposed_entry_can_be_accepted_from_the_editor(page):
    """Editing then accepting in one place, rather than fixing a fact in one panel and
    hunting for it in another."""
    view, _ = page
    await view.evaluate(f"renderEntries({SAMPLE_FACTS})")

    assert await view.query_selector("#entries button[data-save]") is not None
    assert await view.query_selector("#entries button[data-accept]") is not None


def test_the_page_has_no_stray_control_characters():
    """The same class of bug that broke the script: an escaped sequence collapsing into a
    real control character during an edit."""
    text = DASHBOARD.read_text(encoding="utf-8")
    stray = {hex(ord(c)) for c in text if ord(c) < 32 and c not in "\n\t"}
    assert stray == set(), f"control characters in dashboard.html: {stray}"


# --------------------------------------------------------------------------------------
# The Jobs tab
# --------------------------------------------------------------------------------------

SAMPLE_JOBS = """{
  "total": 2, "limit": 50, "offset": 0, "counts": {"recommended": 1},
  "jobs": [
    {"job_id": "aaaaaaaa-0000-0000-0000-000000000001", "title": "AI Engineer",
     "company": "Northwind", "state": "recommended", "match_score": 0.83,
     "recommendation": "apply", "missing_required": ["Kubernetes"], "documents": 0},
    {"job_id": "aaaaaaaa-0000-0000-0000-000000000002",
     "title": "<img src=x onerror=alert(1)>", "company": null, "state": "discovered",
     "match_score": null, "recommendation": null, "missing_required": [], "documents": 0}
  ]
}"""


async def test_there_are_three_tabs_and_only_one_view_shows(page):
    view, errors = page
    tabs = await view.eval_on_selector_all(".tab", "els => els.map(e => e.dataset.view)")
    assert tabs == ["mission", "jobs", "profile"]

    await view.click('.tab[data-view="jobs"]')
    await view.wait_for_timeout(200)
    shown = await view.evaluate(
        "() => [document.querySelector('main'), document.getElementById('jobs-view'),"
        " document.getElementById('profile-view')]"
        ".map(e => getComputedStyle(e).display)"
    )
    assert shown == ["none", "grid", "none"]
    assert _real_errors(errors) == []


async def test_the_jobs_view_is_hidden_in_the_stylesheet_not_only_in_js(page):
    """CSS-first, or the view flashes on screen before the switcher runs."""
    view, _ = page
    assert await view.evaluate(
        "getComputedStyle(document.getElementById('jobs-view')).display"
    ) == "none"


RENDER_BOARD = (
    "data => { document.getElementById('jobs-table').innerHTML ="
    " '<table><tbody>' + data.jobs.map(jobRow).join('') + '</tbody></table>'; }"
)


async def test_the_board_renders_a_row_per_job_with_its_score(page):
    view, errors = page
    await view.evaluate(RENDER_BOARD, json.loads(SAMPLE_JOBS))

    rows = await view.query_selector_all("#jobs-table tbody tr")
    assert len(rows) == 2

    body = await view.eval_on_selector("#jobs-table", "el => el.textContent")
    assert "83%" in body
    # A low score is explained rather than left as a bare number.
    assert "missing Kubernetes" in body
    assert _real_errors(errors) == []


async def test_a_title_written_by_a_stranger_is_escaped(page):
    """Every string on the board came from a job posting."""
    view, errors = page
    await view.evaluate(RENDER_BOARD, json.loads(SAMPLE_JOBS))

    assert await view.query_selector("#jobs-table img") is None
    body = await view.eval_on_selector("#jobs-table", "el => el.textContent")
    assert "<img src=x onerror=alert(1)>" in body, "escaped, not stripped"
    assert _real_errors(errors) == []


async def test_the_board_only_offers_actions_the_api_would_accept(page):
    """A button the API answers with a 409 is worse than no button."""
    view, _ = page
    offered = await view.evaluate(
        "() => Object.fromEntries(Object.entries(JOB_ACTIONS)"
        ".map(([k, v]) => [k, v.map(a => a[0])]))"
    )
    assert offered["recommended"] == ["approve", "cancel"]
    assert offered["user_approved"] == ["documents", "cancel"]
    assert offered["ready_for_browser"] == ["apply", "cancel"]
    # Terminal states offer nothing at all.
    assert "submitted" not in offered
    assert "cancelled" not in offered


async def test_the_add_job_form_is_wired(page):
    view, errors = page
    for selector in ("#job-url", "#job-desc", "#b-add-job", "#job-fetch", "#jobs-table"):
        assert await view.query_selector(selector) is not None, f"{selector} missing"
    assert await view.evaluate(
        "typeof document.getElementById('b-add-job').onclick"
    ) == "function"
    assert _real_errors(errors) == []
