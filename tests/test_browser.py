"""Observer and executor against a real Chromium and the local fixture pages."""

from __future__ import annotations

import pytest
from localapply.contracts import ActionType, Decision, ElementRole, PageKind
from localapply.safety import KILL_SWITCH, AutomationHalted

pytestmark = pytest.mark.browser


@pytest.fixture
async def form_page(browser_manager, observer, apply_url):
    from uuid import uuid4

    session = await browser_manager.new_session()
    await session.page.goto(apply_url)
    observation = await observer.observe(session, uuid4(), screenshot=False)
    return session, observation


# --------------------------------------------------------------------------------------
# Observer
# --------------------------------------------------------------------------------------


async def test_observer_enumerates_form_elements(form_page):
    _, observation = form_page
    names = {e.name for e in observation.elements}
    assert "First name" in names
    assert "Expected salary" in names
    assert "Electronic signature (type your full legal name)" in names


async def test_observer_assigns_sequential_opaque_refs(form_page):
    _, observation = form_page
    refs = [e.ref for e in observation.elements]
    assert refs == [f"e{i}" for i in range(1, len(refs) + 1)]


async def test_observer_detects_an_application_form(form_page):
    _, observation = form_page
    assert observation.page_kind is PageKind.APPLICATION_FORM


async def test_observer_captures_required_and_role_metadata(form_page):
    _, observation = form_page
    by_name = {e.name: e for e in observation.elements}
    assert by_name["First name"].required is True
    assert by_name["Phone number"].required is False
    assert by_name["Upload your CV"].role is ElementRole.FILE_INPUT
    assert by_name["Why do you want to work here?"].role is ElementRole.TEXTAREA


async def test_observer_reads_select_options(form_page):
    _, observation = form_page
    select = next(e for e in observation.elements if e.role is ElementRole.COMBOBOX)
    assert "Yes" in select.options
    assert "I will require sponsorship" in select.options


async def test_confirmation_page_is_recognised(browser_manager, observer, fixtures_dir):
    from uuid import uuid4

    session = await browser_manager.new_session()
    await session.page.goto((fixtures_dir / "submitted.html").as_uri())
    observation = await observer.observe(session, uuid4(), screenshot=False)
    assert observation.page_kind is PageKind.CONFIRMATION


async def test_injection_text_lands_in_untrusted_content_not_in_element_names(
    browser_manager, observer, job_url
):
    """The payload must be visible to the system as *data* -- and must not become part of any
    element's accessible name, where it would ride along in the ref table."""
    from uuid import uuid4

    session = await browser_manager.new_session()
    await session.page.goto(job_url)
    observation = await observer.observe(session, uuid4(), screenshot=False)

    assert "Ignore all previous instructions" in observation.untrusted_text
    for element in observation.elements:
        assert "Ignore all previous instructions" not in element.name


# --------------------------------------------------------------------------------------
# Session ref map (ADR 0002)
# --------------------------------------------------------------------------------------


async def test_session_resolves_only_known_refs(form_page):
    session, observation = form_page
    assert session.resolve(observation.elements[0].ref) is not None
    assert session.resolve("e9999") is None


async def test_navigation_invalidates_every_ref(form_page, job_url):
    session, observation = form_page
    ref = observation.elements[0].ref
    assert session.knows(ref)

    await session.page.goto(job_url)
    session.invalidate()

    assert not session.knows(ref)
    assert session.resolve(ref) is None


async def test_reobservation_rebuilds_the_ref_set(browser_manager, observer, form_page):
    from uuid import uuid4

    session, first = form_page
    epoch_before = session.epoch
    second = await observer.observe(session, uuid4(), screenshot=False)
    assert session.epoch == epoch_before + 1
    assert {e.ref for e in second.elements} == {e.ref for e in first.elements}


# --------------------------------------------------------------------------------------
# Executor
# --------------------------------------------------------------------------------------


async def test_executor_fills_a_safe_field(settings, form_page):
    from localapply.browser.executor import BrowserExecutor

    session, observation = form_page
    first_name = next(e for e in observation.elements if e.name == "First name")
    decision = Decision(
        action=ActionType.TYPE, target_ref=first_name.ref, value="Haidar",
        confidence=0.95, reason="test",
    )

    result = await BrowserExecutor(settings).execute(decision, observation, session)

    assert result.success
    assert await session.page.input_value("#first_name") == "Haidar"


async def test_executor_refuses_an_unknown_ref(settings, form_page):
    """Defence in depth: policy rejects this first, but the executor must too."""
    from localapply.browser.executor import BrowserExecutor

    session, observation = form_page
    decision = Decision(
        action=ActionType.TYPE, target_ref="e9999", value="x", confidence=1.0, reason="hostile"
    )

    result = await BrowserExecutor(settings).execute(decision, observation, session)

    assert result.success is False
    assert "not resolvable" in (result.error or "")


async def test_kill_switch_stops_the_executor_dead(settings, form_page):
    from localapply.browser.executor import BrowserExecutor

    session, observation = form_page
    decision = Decision(
        action=ActionType.TYPE, target_ref=observation.elements[0].ref, value="x",
        confidence=1.0, reason="test",
    )

    KILL_SWITCH.engage("test")
    with pytest.raises(AutomationHalted):
        await BrowserExecutor(settings).execute(decision, observation, session)


async def test_submit_is_simulated_under_dry_run(settings, form_page):
    """The decision, the approval, and the log entry are all real. Only the click is withheld."""
    from localapply.browser.executor import BrowserExecutor

    assert settings.dry_run is True
    session, observation = form_page
    button = next(e for e in observation.elements if "Submit" in e.name)
    decision = Decision(
        action=ActionType.SUBMIT, target_ref=button.ref, confidence=1.0, reason="test"
    )

    result = await BrowserExecutor(settings).execute(decision, observation, session)

    assert result.success and result.simulated
    # Still on the form. The confirmation page was never reached.
    assert "apply.html" in session.page.url


# --------------------------------------------------------------------------------------
# What real job boards actually look like
#
# Everything above this line is verified against `evaluation/fixtures/`, a page we wrote.
# Running the observer against real Greenhouse, Lever and Ashby pages found two ways the
# heuristics were wrong -- and the first of them made the agent useless on every real site
# it would ever be pointed at.
# --------------------------------------------------------------------------------------

from localapply.browser.observer import MANY_LINKS, infer_page_kind
from localapply.contracts import ObservedElement


def el(ref: str, role: ElementRole, name: str = "", **kw) -> ObservedElement:
    return ObservedElement(ref=ref, role=role, name=name, **kw)


def test_an_invisible_recaptcha_is_not_a_wall():
    """THE finding. Every modern application form loads reCAPTCHA v3, which runs invisibly
    and asks the visitor for nothing. "Is there a recaptcha iframe" was true on every real
    Greenhouse and Ashby form tested, so every one of them was refused as a CAPTCHA -- and
    a CAPTCHA parks the job and hands it to a human, which means the agent never worked on
    a real site at all.

    The frame test now lives in the page script; this pins the consequence.
    """
    form = [el(f"e{i}", ElementRole.TEXTBOX, "First name") for i in range(4)]
    assert infer_page_kind(
        "https://job-boards.greenhouse.io/vercel/jobs/1",
        "Job Application for Account Executive at Vercel",
        "Apply for this job. This site is protected by reCAPTCHA and the Google "
        "Privacy Policy and Terms of Service apply.",
        form,
        captcha_frame=False,
    ) is PageKind.APPLICATION_FORM


def test_boilerplate_mentioning_recaptcha_is_not_a_challenge():
    """The footer of essentially every form on the internet."""
    assert infer_page_kind(
        "https://example.com/apply",
        "Apply",
        "This site is protected by reCAPTCHA and the Google Privacy Policy applies.",
        [el("e1", ElementRole.TEXTBOX, "Email")],
        captcha_frame=False,
    ) is not PageKind.CAPTCHA


@pytest.mark.parametrize(
    "text",
    ["Please confirm you're not a robot to continue",
     "Verify you are human before submitting",
     "Select all images with traffic lights"],
)
def test_an_actual_challenge_is_still_a_wall(text):
    """Narrowing the pattern must not blind it. Getting past one of these is the person's
    decision to make in their own browser."""
    assert infer_page_kind(
        "https://example.com/apply", "Apply", text, [], captcha_frame=False
    ) is PageKind.CAPTCHA


def test_a_captcha_frame_still_wins_over_everything():
    assert infer_page_kind(
        "https://example.com/apply", "Apply", "Nothing suspicious here",
        [el("e1", ElementRole.TEXTBOX, "Email")], captcha_frame=True,
    ) is PageKind.CAPTCHA


def test_a_board_full_of_filter_dropdowns_is_a_listing_not_a_form():
    """Verified on jobs.ashbyhq.com/linear: four comboboxes -- Department, Employment,
    Location, Location Type -- and thirty-seven job links. "Three or more fillable things"
    called that an application form, and the run loop advances to FORM_ANALYZED on that."""
    page = [
        el("e1", ElementRole.COMBOBOX, "Department"),
        el("e2", ElementRole.COMBOBOX, "Employment"),
        el("e3", ElementRole.COMBOBOX, "Location"),
        el("e4", ElementRole.COMBOBOX, "Location Type"),
        *[el(f"e{i}", ElementRole.LINK, "Senior Engineer") for i in range(5, 5 + MANY_LINKS)],
    ]
    assert infer_page_kind(
        "https://jobs.ashbyhq.com/linear", "Linear Jobs", "Open roles", page,
        captcha_frame=False,
    ) is PageKind.JOB_LISTING


def test_a_listing_is_recognised_even_though_no_link_says_the_word_job():
    """Real listing links are job *titles*. The old rule looked for "job" in the link text,
    which is why a real Greenhouse board came back UNKNOWN."""
    page = [
        el("e1", ElementRole.TEXTBOX, "Search"),
        *[el(f"e{i}", ElementRole.LINK, "Account Executive, Commercial")
          for i in range(2, 2 + MANY_LINKS)],
    ]
    assert infer_page_kind(
        "https://job-boards.greenhouse.io/vercel", "Jobs at Vercel", "", page,
        captcha_frame=False,
    ) is PageKind.JOB_LISTING


def test_a_real_form_is_not_mistaken_for_a_listing():
    """The other direction. A form has a handful of navigation links and plenty to type in."""
    page = [
        el("e1", ElementRole.LINK, "Back to jobs"),
        el("e2", ElementRole.TEXTBOX, "First Name"),
        el("e3", ElementRole.TEXTBOX, "Last Name"),
        el("e4", ElementRole.TEXTBOX, "Email"),
        el("e5", ElementRole.FILE_INPUT, "Resume"),
    ]
    assert infer_page_kind(
        "https://job-boards.greenhouse.io/vercel/jobs/1", "Job Application", "", page,
        captcha_frame=False,
    ) is PageKind.APPLICATION_FORM
