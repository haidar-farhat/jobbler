"""The walking skeleton, end to end.

One run drives every layer: observer, reasoner, policy, executor, event stream, approvals,
state machine, kill switch. If this passes, the architecture holds together.
"""

from __future__ import annotations

import asyncio
import time
from uuid import UUID

import pytest
from sqlmodel import select

from localapply.contracts import EventType
from localapply.db import models as m
from localapply.db.session import session_factory
from localapply.orchestrator.state_machine import ApplicationState
from localapply.safety import KILL_SWITCH

pytestmark = pytest.mark.browser

TERMINAL = {"finished", "failed", "stopped"}


async def drive(run_manager, handle, *, approve=True, timeout=90.0):
    """Play the human: resolve approvals as they appear, until the run ends.

    Returns the approvals that were requested, in order.
    """
    seen: list = []
    resolved: set[UUID] = set()
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        if handle.status in TERMINAL:
            return seen

        pending = handle.pending
        if pending is not None and pending.approval_id not in resolved:
            seen.append(pending)
            resolved.add(pending.approval_id)
            await run_manager.resolve_approval(
                handle.run_id, pending.approval_id, approved=approve
            )
        await asyncio.sleep(0.05)

    raise TimeoutError(f"Run did not finish within {timeout}s (status={handle.status})")


async def submit_actions(run_id: UUID) -> list[m.BrowserAction]:
    async with session_factory()() as session:
        result = await session.execute(
            select(m.BrowserAction).where(
                m.BrowserAction.run_id == run_id, m.BrowserAction.action == "submit"
            )
        )
        return list(result.scalars().all())


async def all_actions(run_id: UUID) -> list[m.BrowserAction]:
    async with session_factory()() as session:
        result = await session.execute(
            select(m.BrowserAction)
            .where(m.BrowserAction.run_id == run_id)
            .order_by(m.BrowserAction.ts)
        )
        return list(result.scalars().all())


@pytest.fixture
async def started_run(run_manager, seeded_context, job_url):
    handle = await run_manager.start(
        start_url=job_url, goal="Apply for the AI Engineer role", reasoning=seeded_context
    )
    yield handle
    if handle.status not in TERMINAL:
        await run_manager.stop(handle.run_id, "test teardown")


# --------------------------------------------------------------------------------------
# The full happy path
# --------------------------------------------------------------------------------------


async def test_full_run_reaches_submitted_after_approval(
    run_manager, started_run, event_bus
):
    approvals = await drive(run_manager, started_run)

    assert started_run.status == "finished"
    assert started_run.state is ApplicationState.SUBMITTED

    # The submit was gated, and was simulated rather than really clicked.
    submits = await submit_actions(started_run.run_id)
    assert len(submits) == 1
    assert submits[0].simulated is True
    assert submits[0].policy_rule == "R000_HUMAN_APPROVED"

    # Every approval the user saw was raised by a named policy rule.
    assert approvals, "the run should have stopped for the user at least once"
    assert any(p.decision.action.value == "submit" for p in approvals)


async def test_run_navigated_from_the_listing_into_the_form(run_manager, started_run):
    await drive(run_manager, started_run)
    actions = await all_actions(started_run.run_id)
    kinds = [a.action for a in actions]

    assert kinds[0] == "navigate"          # opening the job page
    assert "click" in kinds                # "Apply for this role"
    assert "type" in kinds                 # filling the form
    assert kinds[-1] == "submit"


async def test_safe_fields_were_filled_from_the_verified_profile(run_manager, started_run):
    await drive(run_manager, started_run)
    actions = await all_actions(started_run.run_id)
    filled = {a.target_name: a.value for a in actions if a.action == "type" and a.success}

    assert filled.get("First name") == "Haidar"
    assert filled.get("Email address") == "you@example.com"


async def test_event_stream_records_the_whole_loop(run_manager, started_run, event_bus):
    await drive(run_manager, started_run)
    types = [e.type for e in event_bus.history(started_run.run_id)]

    for expected in (
        EventType.RUN_STARTED,
        EventType.OBSERVATION,
        EventType.DECISION,
        EventType.POLICY_VERDICT,
        EventType.ACTION_RESULT,
        EventType.APPROVAL_REQUESTED,
        EventType.APPROVAL_RESOLVED,
        EventType.STATE_CHANGED,
        EventType.RUN_FINISHED,
    ):
        assert expected in types, f"missing {expected.value} in the event log"

    # Sequence numbers are monotonic, so the UI can order and resume from them.
    seqs = [e.seq for e in event_bus.history(started_run.run_id)]
    assert seqs == sorted(seqs) == list(range(1, len(seqs) + 1))


async def test_observation_events_carry_the_full_element_table(
    run_manager, started_run, event_bus
):
    """Every ref named in a later event must stay resolvable when replaying the run."""
    await drive(run_manager, started_run)
    history = event_bus.history(started_run.run_id)

    observations = [e for e in history if e.type is EventType.OBSERVATION]
    assert observations
    assert all("elements" in e.payload for e in observations)

    form_view = next(
        e for e in observations if e.payload["page_kind"] == "application_form"
    )
    names = {el["name"] for el in form_view.payload["elements"]}
    assert "Expected salary" in names


# --------------------------------------------------------------------------------------
# The approval gate
# --------------------------------------------------------------------------------------


async def test_run_halts_for_approval_before_any_submit(run_manager, started_run):
    """The core human-in-the-loop guarantee: while the run is parked on an approval, nothing
    has been submitted."""
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if started_run.pending is not None:
            break
        if started_run.status in TERMINAL:
            pytest.fail("run finished without ever asking for approval")
        await asyncio.sleep(0.05)
    else:
        pytest.fail("run never requested approval")

    assert started_run.status == "waiting_approval"
    assert started_run.state is ApplicationState.REVIEW_REQUIRED
    assert await submit_actions(started_run.run_id) == []


async def test_first_approval_is_the_salary_field(run_manager, started_run):
    """Every safe field is filled without interruption; the first stop is the first field
    whose value is a negotiating position."""
    deadline = time.monotonic() + 60
    while started_run.pending is None and time.monotonic() < deadline:
        if started_run.status in TERMINAL:
            pytest.fail("run finished without asking")
        await asyncio.sleep(0.05)

    pending = started_run.pending
    assert pending is not None

    async with session_factory()() as session:
        approval = await session.get(m.Approval, pending.approval_id)

    assert approval.target_name == "Expected salary"
    assert approval.policy_rule == "R011_REVIEW_REQUIRED_FIELD"
    assert approval.field_class == "review_required"
    assert approval.proposed_value == "USD 4,500 / month"


async def test_rejecting_every_approval_never_submits(run_manager, started_run):
    await drive(run_manager, started_run, approve=False)

    assert started_run.state is not ApplicationState.SUBMITTED
    assert await submit_actions(started_run.run_id) == []


async def test_signature_field_is_never_touched(run_manager, started_run):
    """NEVER_AUTOFILL means no action is even proposed -- the field is left for the human."""
    await drive(run_manager, started_run)
    actions = await all_actions(started_run.run_id)

    for action in actions:
        assert "signature" not in (action.target_name or "").lower()
        assert "birth" not in (action.target_name or "").lower()


async def test_user_is_told_which_fields_were_left_for_them(
    run_manager, started_run, event_bus
):
    await drive(run_manager, started_run)
    logs = [e for e in event_bus.history(started_run.run_id) if e.type is EventType.LOG]

    warning = next((e for e in logs if "Left for you" in e.message), None)
    assert warning is not None, "the run should say which required fields it would not fill"
    assert any("signature" in f.lower() for f in warning.payload["fields"])


# --------------------------------------------------------------------------------------
# Injection, at the level of a real run
# --------------------------------------------------------------------------------------


async def test_injected_pages_do_not_shortcut_the_approval_gate(run_manager, started_run):
    """Both fixture pages carry payloads telling the agent that approval was already granted
    and to submit immediately. The run must still stop and ask."""
    approvals = await drive(run_manager, started_run)

    submit_approvals = [p for p in approvals if p.decision.action.value == "submit"]
    assert len(submit_approvals) == 1

    submits = await submit_actions(started_run.run_id)
    assert len(submits) == 1
    assert submits[0].policy_rule == "R000_HUMAN_APPROVED"


# --------------------------------------------------------------------------------------
# Kill switch
# --------------------------------------------------------------------------------------


async def test_kill_switch_stops_a_run_in_flight(run_manager, started_run):
    # Let the run get going.
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if len(await all_actions(started_run.run_id)) >= 2:
            break
        await asyncio.sleep(0.05)

    before = len(await all_actions(started_run.run_id))
    await run_manager.stop_all("test kill switch")

    assert KILL_SWITCH.engaged
    await asyncio.sleep(0.5)
    after = len(await all_actions(started_run.run_id))

    assert after == before, "no further actions may execute once the kill switch is engaged"
    assert started_run.status == "stopped"


async def test_new_runs_are_refused_while_stopped(run_manager, seeded_context, job_url):
    from localapply.safety import AutomationHalted

    KILL_SWITCH.engage("test")
    with pytest.raises(AutomationHalted):
        await run_manager.start(start_url=job_url, goal="x", reasoning=seeded_context)


# --------------------------------------------------------------------------------------
# Pause / resume
# --------------------------------------------------------------------------------------


async def test_pause_stops_between_actions_not_mid_action(run_manager, started_run):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and len(await all_actions(started_run.run_id)) < 2:
        await asyncio.sleep(0.05)

    await run_manager.pause(started_run.run_id)
    assert started_run.status == "paused"

    await asyncio.sleep(0.4)
    settled = len(await all_actions(started_run.run_id))
    await asyncio.sleep(0.4)
    assert len(await all_actions(started_run.run_id)) == settled

    await run_manager.resume(started_run.run_id)
    assert started_run.status == "running"
