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
