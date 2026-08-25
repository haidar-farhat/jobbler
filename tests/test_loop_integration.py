"""The walking skeleton, end to end.

One run drives every layer: observer, reasoner, policy, executor, event stream, approvals,
state machine, kill switch. If this passes, the architecture holds together.
"""

from __future__ import annotations

import asyncio
import time
from uuid import UUID

import pytest
from localapply.contracts import EventType
from localapply.db import models as m
from localapply.db.session import session_factory
from localapply.orchestrator.state_machine import ApplicationState
from localapply.safety import KILL_SWITCH
from sqlmodel import select

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

    await run_manager.stop_all("test kill switch")
    assert KILL_SWITCH.engaged

    # Sample *after* the switch is engaged, not before. An action already in flight when the
    # button is pressed is allowed to finish -- the guarantee is that no *new* action starts,
    # and sampling beforehand raced that legitimate in-flight completion.
    settled = len(await all_actions(started_run.run_id))
    await asyncio.sleep(0.6)

    assert len(await all_actions(started_run.run_id)) == settled, (
        "no further actions may start once the kill switch is engaged"
    )
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


# --------------------------------------------------------------------------------------
# Durable audit trail
# --------------------------------------------------------------------------------------


async def stored_events(run_id: UUID) -> list[m.AgentEventRow]:
    async with session_factory()() as session:
        result = await session.execute(
            select(m.AgentEventRow)
            .where(m.AgentEventRow.run_id == run_id)
            .order_by(m.AgentEventRow.seq)
        )
        return list(result.scalars().all())


async def test_events_are_persisted_not_just_streamed(run_manager, started_run, event_bus):
    """The event log is the audit trail and the basis for replay, so it has to survive a
    restart. An earlier version published to the in-memory bus only, leaving `agent_events`
    empty while the docs claimed otherwise."""
    await drive(run_manager, started_run)

    rows = await stored_events(started_run.run_id)
    streamed = event_bus.history(started_run.run_id)

    assert rows, "agent_events must not be empty after a run"
    assert len(rows) == len(streamed)
    assert [r.seq for r in rows] == list(range(1, len(rows) + 1))

    types = {r.type for r in rows}
    assert {"observation", "decision", "policy_verdict", "action_result"} <= types


async def test_persisted_events_can_reconstruct_a_ref(run_manager, started_run):
    """Replay test: a ref named in a stored decision must resolve against the element table
    stored with the observation that preceded it."""
    await drive(run_manager, started_run)
    rows = await stored_events(started_run.run_id)

    elements: dict[str, str] = {}
    resolved = 0
    for row in rows:
        if row.type == "observation":
            elements = {e["ref"]: e["name"] for e in row.payload["elements"]}
        elif row.type == "decision" and row.payload.get("target_ref"):
            assert row.payload["target_ref"] in elements, (
                f"ref {row.payload['target_ref']} unresolvable from the stored element table"
            )
            resolved += 1

    assert resolved > 5, "expected several targeted decisions to replay"


async def test_the_warning_about_hand_filled_fields_is_persisted(run_manager, started_run):
    await drive(run_manager, started_run)
    rows = await stored_events(started_run.run_id)

    warnings = [r for r in rows if r.type == "log" and "Left for you" in r.message]
    assert warnings, "the hand-fill warning must reach the durable log, not only the UI"
    assert any("signature" in f.lower() for f in warnings[0].payload["fields"])


# --------------------------------------------------------------------------------------
# Asking for help must terminate
#
# Found by driving a real run: the reasoner returned ASK_USER, the human approved without
# changing anything in the browser, the page was therefore identical, and the reasoner
# asked again. The run cycled form_analyzed -> blocked -> user_intervention -> form_analyzed
# indefinitely with `actions_executed` frozen, because an ASK_USER never reaches the
# executor and so never spends the action budget.
# --------------------------------------------------------------------------------------


def observation_with(url: str, names: list[str]):
    from uuid import uuid4

    from localapply.contracts import ElementRole, Observation, ObservedElement

    return Observation(
        run_id=uuid4(),
        url=url,
        title="Apply",
        elements=[
            ObservedElement(ref=f"e{i}", role=ElementRole.TEXTBOX, name=name)
            for i, name in enumerate(names, start=1)
        ],
    )


def test_the_page_key_ignores_refs_but_notices_the_page_changing():
    """Refs are reassigned on every observation (ADR 0002). Keying on them would make every
    page look new and the guard would never fire."""
    from localapply.orchestrator.run_loop import help_page_key

    first = observation_with("https://x/apply", ["Email", "Phone"])
    again = observation_with("https://x/apply", ["Email", "Phone"])
    assert help_page_key(first) == help_page_key(again)

    solved = observation_with("https://x/apply", ["Email", "Phone", "Submit application"])
    assert help_page_key(first) != help_page_key(solved)
    assert help_page_key(first) != help_page_key(observation_with("https://x/done", ["Email", "Phone"]))


def test_repeated_help_about_an_unchanged_page_stops_the_run(run_manager):
    """The regression, as a pure unit check on the guard."""
    from uuid import uuid4

    from localapply.orchestrator.run_loop import MAX_HELP_AT_ONE_PAGE, RunHandle

    handle = RunHandle(run_id=uuid4(), application_id=None, goal="apply", start_url="https://x")
    page = observation_with("https://x/apply", ["Email"])

    for _ in range(MAX_HELP_AT_ONE_PAGE):
        assert run_manager._help_would_repeat(handle, page) is None, "asking is normal"

    stuck = run_manager._help_would_repeat(handle, page)
    assert stuck is not None, "an unchanged page must not be asked about forever"
    assert "nothing on it changed" in stuck


def test_help_after_the_page_actually_changes_is_not_a_loop(run_manager):
    """Resolving a CAPTCHA changes the page. The agent must be free to ask again."""
    from uuid import uuid4

    from localapply.orchestrator.run_loop import MAX_HELP_AT_ONE_PAGE, RunHandle

    handle = RunHandle(run_id=uuid4(), application_id=None, goal="apply", start_url="https://x")

    for step in range(MAX_HELP_AT_ONE_PAGE + 3):
        page = observation_with("https://x/apply", [f"Question {step}"])
        assert run_manager._help_would_repeat(handle, page) is None


def test_a_page_that_changes_every_time_still_cannot_ask_forever(run_manager):
    """A carousel or a clock resets the per-page counter on every observation."""
    from uuid import uuid4

    from localapply.orchestrator.run_loop import MAX_HELP_REQUESTS, RunHandle

    handle = RunHandle(run_id=uuid4(), application_id=None, goal="apply", start_url="https://x")

    results = [
        run_manager._help_would_repeat(handle, observation_with("https://x/a", [f"t{i}"]))
        for i in range(MAX_HELP_REQUESTS + 2)
    ]
    assert results[-1] is not None
    assert "more than this step should ever need" in results[-1]
