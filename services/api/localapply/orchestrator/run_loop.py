"""The Observe -> Reason -> Policy -> Execute loop, and the manager that owns running loops.

This is the only module that writes `applications.state`, and it does so exclusively through
`state_machine.transition()`.

Control flow worth knowing:

  * **Pause** is an `asyncio.Event` awaited at the top of each iteration, so a paused run stops
    between actions rather than mid-click.
  * **Approval** parks the loop on an event that the API resolves. The run holds its browser
    session open while it waits, so approving from a phone resumes the exact same page.
  * **Kill switch** is checked here *and* in the executor. Belt and braces on the one control
    that must never fail.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import traceback
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlmodel import select

from ..ai.reasoner import Reasoner, ReasoningContext, normalise
from ..browser.executor import BrowserExecutor
from ..browser.observer import Observer
from ..browser.session import BrowserManager, BrowserSession
from ..config import Settings
from ..contracts import (
    ActionResult,
    ActionType,
    Decision,
    EventType,
    Observation,
    PolicyOutcome,
)
from ..db import models as m
from ..db.session import session_factory
from ..events.bus import EventBus
from ..policy.capabilities import capabilities_for
from ..policy.engine import PolicyEngine
from ..policy.field_classifier import FieldClass, classify
from ..policy.rules import RunContext, action_signature, decision_fingerprint
from ..safety import KILL_SWITCH, AutomationHalted
from .state_machine import ApplicationState, InvalidTransition, transition

logger = logging.getLogger(__name__)

S = ApplicationState

#: How many times the agent may ask for help at a page that has not changed since it last
#: asked. An ASK_USER is not an action, so it never reaches the executor and the action
#: budget cannot bound it -- and a live run against the fixture proved what that costs: the
#: model asked, the human approved, the page was identical, so the model asked again. The
#: run cycled form_analyzed -> blocked -> user_intervention -> form_analyzed forever with
#: `actions_executed` frozen at 13, and only a human noticing could stop it.
#:
#: One retry is deliberate. A person may approve a moment before the page settles, and
#: failing on the first repeat would turn a mistimed click into a dead run.
MAX_HELP_AT_ONE_PAGE = 2

#: A backstop for a page that changes every observation -- a carousel, a clock, a spinner --
#: which resets the per-page counter every time and would otherwise ask forever.
MAX_HELP_REQUESTS = 12


def help_page_key(observation: Observation) -> str:
    """What the agent is looking at, for deciding whether help changed anything.

    Element *refs* are deliberately excluded: the observer reassigns them on every
    observation (ADR 0002), so including them would make every page look new and the guard
    would never fire. Role, name and enabled state are what actually change when a person
    solves a CAPTCHA or dismisses a login wall.
    """
    shape = sorted(
        f"{e.role.value}:{e.name}:{int(e.enabled)}:{int(e.visible)}" for e in observation.elements
    )
    return "|".join([observation.url, observation.page_kind.value, *shape])


def _known_values(reasoning: ReasoningContext) -> set[str]:
    """Every value the agent is permitted to enter, normalised for comparison.

    Drawn from accepted facts only -- `load_reasoning_context` has already filtered out
    proposals -- so this doubles as the boundary that keeps an unreviewed CV extraction out
    of a form.
    """
    values = list(reasoning.profile.values()) + list(reasoning.drafts.values())
    known: set[str] = set()
    for value in values:
        if not value:
            continue
        known.add(" ".join(value.split()).casefold())
    return known


@dataclass
class PendingApproval:
    approval_id: UUID
    decision: Decision
    event: asyncio.Event = field(default_factory=asyncio.Event)
    approved: bool = False
    edited_value: str | None = None


@dataclass
class RunHandle:
    run_id: UUID
    application_id: UUID | None
    goal: str
    start_url: str
    status: str = "running"  # running | paused | waiting_approval | finished | failed | stopped
    state: ApplicationState = S.READY_FOR_BROWSER
    reasoning: ReasoningContext = field(default_factory=ReasoningContext)
    policy: RunContext = field(default_factory=lambda: RunContext("application", frozenset()))
    pending: PendingApproval | None = None
    #: Set means "running". Cleared means "paused".
    resume_signal: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    session: BrowserSession | None = None
    error: str | None = None
    #: Where the agent last asked for help, and how many times it has asked there. An
    #: ASK_USER never reaches the executor, so the action budget cannot bound it -- see
    #: MAX_HELP_AT_ONE_PAGE.
    last_help_page: str | None = None
    help_at_this_page: int = 0
    help_requests: int = 0

    def snapshot(self) -> dict:
        return {
            "run_id": str(self.run_id),
            "application_id": str(self.application_id) if self.application_id else None,
            "status": self.status,
            "state": self.state.value,
            "goal": self.goal,
            "start_url": self.start_url,
            "actions_executed": self.policy.actions_executed,
            "max_actions": self.policy.max_actions,
            "error": self.error,
            "pending_approval": str(self.pending.approval_id) if self.pending else None,
        }


class RunManager:
    """Owns every in-flight run. One instance per process, built in the composition root."""

    def __init__(
        self,
        *,
        settings: Settings,
        browser: BrowserManager,
        observer: Observer,
        reasoner: Reasoner,
        policy: PolicyEngine,
        executor: BrowserExecutor,
        bus: EventBus,
    ) -> None:
        self._settings = settings
        self._browser = browser
        self._observer = observer
        self._reasoner = reasoner
        self._policy = policy
        self._executor = executor
        self._bus = bus
        self._runs: dict[UUID, RunHandle] = {}
        # `agent_events` is the durable audit trail and the source for post-hoc replay.
        # Without this the stream would exist only in memory and vanish on restart.
        bus.attach_sink(self._persist_event)

    async def _persist_event(self, event) -> None:
        await self._save(
            m.AgentEventRow(
                id=event.event_id,
                run_id=event.run_id,
                seq=event.seq,
                type=event.type.value,
                agent=event.agent,
                message=event.message,
                payload=event.payload,
                ts=event.ts,
            )
        )

    # -- lifecycle ----------------------------------------------------------------------

    @property
    def runs(self) -> dict[UUID, RunHandle]:
        return self._runs

    @property
    def browser(self) -> BrowserManager:
        return self._browser

    @property
    def settings(self) -> Settings:
        return self._settings

    def get(self, run_id: UUID) -> RunHandle | None:
        return self._runs.get(run_id)

    async def start(
        self,
        *,
        start_url: str,
        goal: str,
        reasoning: ReasoningContext,
        application_id: UUID | None = None,
        agent: str = "application",
    ) -> RunHandle:
        if KILL_SWITCH.engaged:
            raise AutomationHalted(KILL_SWITCH.reason or "Automation is stopped.")

        handle = RunHandle(
            run_id=uuid4(),
            application_id=application_id,
            goal=goal,
            start_url=start_url,
            reasoning=reasoning,
            policy=RunContext(
                agent=agent,
                capabilities=capabilities_for(agent),
                max_actions=self._settings.max_actions_per_run,
                min_confidence=self._settings.min_decision_confidence,
                dry_run=self._settings.dry_run,
                known_values=_known_values(reasoning),
            ),
        )
        handle.resume_signal.set()
        self._runs[handle.run_id] = handle

        await self._save(
            m.AgentRun(
                id=handle.run_id,
                application_id=application_id,
                agent=agent,
                goal=goal,
                status="running",
                start_url=start_url,
            )
        )
        handle.task = asyncio.create_task(self._run(handle))
        return handle

    async def pause(self, run_id: UUID) -> None:
        handle = self._require(run_id)
        handle.resume_signal.clear()
        handle.status = "paused"
        await self._bus.emit(run_id, EventType.RUN_PAUSED, "Paused. No new actions will start.")

    async def resume(self, run_id: UUID) -> None:
        handle = self._require(run_id)
        handle.resume_signal.set()
        if handle.status == "paused":
            handle.status = "running"
        await self._bus.emit(run_id, EventType.RUN_RESUMED, "Resumed.")

    async def stop(self, run_id: UUID, reason: str = "Stopped by user") -> None:
        handle = self._require(run_id)
        handle.status = "stopped"
        handle.resume_signal.set()  # Unblock so the loop can notice and exit.
        if handle.pending is not None:
            handle.pending.approved = False
            handle.pending.event.set()
        if handle.task is not None and not handle.task.done():
            handle.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await handle.task
        await self._finish(handle, "stopped", reason)

    async def stop_all(self, reason: str = "Kill switch engaged") -> None:
        """The big red button. Engages the global kill switch, then unwinds every run."""
        KILL_SWITCH.engage(reason)
        for run_id in list(self._runs):
            await self._bus.emit(run_id, EventType.KILL_SWITCH, reason)
            with contextlib.suppress(KeyError):
                await self.stop(run_id, reason)
        await self._browser.stop()

    async def resolve_approval(
        self,
        run_id: UUID,
        approval_id: UUID,
        *,
        approved: bool,
        edited_value: str | None = None,
        actor: str = "user",
        source: str = "web",
    ) -> None:
        handle = self._require(run_id)
        pending = handle.pending
        if pending is None or pending.approval_id != approval_id:
            raise KeyError(f"No pending approval {approval_id} on run {run_id}.")

        pending.approved = approved
        pending.edited_value = edited_value

        await self._update_approval(
            approval_id,
            status="approved" if approved else "rejected",
            edited_value=edited_value,
            resolved_by=f"{actor}@{source}",
        )
        await self._save(
            m.AuditLog(
                actor=actor,
                source=source,
                action="approve" if approved else "reject",
                target=str(approval_id),
                detail={"run_id": str(run_id), "edited": edited_value is not None},
            )
        )
        await self._bus.emit(
            run_id,
            EventType.APPROVAL_RESOLVED,
            f"{'Approved' if approved else 'Rejected'} by {actor} ({source}).",
            payload={"approval_id": str(approval_id), "approved": approved},
        )
        pending.event.set()

    def _require(self, run_id: UUID) -> RunHandle:
        handle = self._runs.get(run_id)
        if handle is None:
            raise KeyError(f"Unknown run {run_id}.")
        return handle

    # -- the loop -----------------------------------------------------------------------

    async def _run(self, handle: RunHandle) -> None:
        try:
            handle.session = await self._browser.new_session()
            await self._save(
                m.BrowserSessionRow(
                    id=handle.session.session_id,
                    run_id=handle.run_id,
                    start_url=handle.start_url,
                )
            )
            await self._bus.emit(
                handle.run_id,
                EventType.RUN_STARTED,
                f"Run started: {handle.goal}",
                payload={"start_url": handle.start_url, "dry_run": self._settings.dry_run},
            )
            await self._transition(handle, S.BROWSER_RUNNING)

            # The opening navigation goes through the same Decision -> Policy -> Execute path
            # as everything else, so it is logged and governed identically.
            await self._step(
                handle,
                Observation(run_id=handle.run_id, url="about:blank", title=""),
                Decision(
                    action=ActionType.NAVIGATE,
                    value=handle.start_url,
                    confidence=1.0,
                    reason="Opening the target page.",
                ),
            )

            while True:
                if KILL_SWITCH.engaged:
                    await self._bus.emit(
                        handle.run_id, EventType.KILL_SWITCH, "Automation stopped."
                    )
                    await self._finish(handle, "stopped", KILL_SWITCH.reason or "Kill switch")
                    return

                await handle.resume_signal.wait()
                if handle.status == "stopped":
                    return

                if handle.policy.actions_executed >= handle.policy.max_actions:
                    await self._bus.emit(
                        handle.run_id,
                        EventType.RUN_FAILED,
                        f"Action budget of {handle.policy.max_actions} exhausted.",
                    )
                    await self._finish(handle, "failed", "Action budget exhausted")
                    return

                observation = await self._observe(handle)
                decision = await self._reasoner.decide(observation, handle.reasoning)
                await self._bus.emit(
                    handle.run_id,
                    EventType.DECISION,
                    f"{decision.action.value}: {decision.reason}",
                    agent=self._reasoner.name,
                    payload=decision.model_dump(mode="json"),
                )

                if decision.action is ActionType.FINISH:
                    await self._bus.emit(handle.run_id, EventType.RUN_FINISHED, decision.reason)
                    await self._finish(handle, "finished", decision.reason)
                    return

                if decision.action is ActionType.ASK_USER:
                    stuck = self._help_would_repeat(handle, observation)
                    if stuck is not None:
                        await self._bus.emit(handle.run_id, EventType.RUN_FAILED, stuck)
                        await self._finish(handle, "failed", stuck)
                        return

                    # Raise a real approval rather than pausing silently. An earlier version
                    # just set the status and waited, so the dashboard showed
                    # "waiting_approval" with nothing to act on and the run sat there
                    # forever. Approving resumes; rejecting ends the run.
                    await self._transition(handle, S.BLOCKED)
                    await self._transition(handle, S.USER_INTERVENTION)
                    resume = await self._ask_for_help(handle, observation, decision)
                    if not resume:
                        await self._bus.emit(
                            handle.run_id, EventType.RUN_FINISHED, "Stopped at your request."
                        )
                        await self._finish(handle, "stopped", "user declined to continue")
                        return
                    await self._transition(handle, S.FORM_ANALYZED, tolerant=True)
                    continue

                finished = await self._step(handle, observation, decision)
                if finished:
                    return

        except asyncio.CancelledError:
            # Awaiting during cancellation can itself be interrupted; cleanup is best-effort
            # here because `stop()` closes the session on the caller's side too.
            with contextlib.suppress(Exception):
                await self._close_session(handle)
            raise
        except AutomationHalted as exc:
            await self._bus.emit(handle.run_id, EventType.KILL_SWITCH, str(exc))
            await self._finish(handle, "stopped", str(exc))
        except Exception as exc:  # noqa: BLE001 - the loop must never die silently
            # Include the traceback. An earlier version emitted only "ClassName: str(exc)",
            # which for a bare `raise SomeError` renders as "SomeError:" and says nothing
            # about where it came from.
            trace = "".join(traceback.format_exception(exc))
            handle.error = f"{exc.__class__.__name__}: {exc}".rstrip(": ")
            logger.exception("run %s failed", handle.run_id)
            await self._bus.emit(
                handle.run_id,
                EventType.RUN_FAILED,
                handle.error,
                payload={"traceback": trace[-4000:]},
            )
            await self._finish(handle, "failed", handle.error)

    async def _observe(self, handle: RunHandle) -> Observation:
        assert handle.session is not None
        observation = await self._observer.observe(handle.session, handle.run_id)

        if observation.screenshot_id is not None:
            await self._save(
                m.Screenshot(
                    id=observation.screenshot_id,
                    run_id=handle.run_id,
                    path=str(self._settings.screenshot_dir / f"{observation.screenshot_id}.png"),
                    url=observation.url,
                )
            )

        await self._bus.emit(
            handle.run_id,
            EventType.OBSERVATION,
            f"{observation.page_kind.value} — {observation.title or observation.url}",
            agent="observer",
            payload={
                "url": observation.url,
                "title": observation.title,
                "page_kind": observation.page_kind.value,
                "screenshot_id": str(observation.screenshot_id)
                if observation.screenshot_id
                else None,
                "element_count": len(observation.elements),
                # The full element table is logged so any ref in a later event stays
                # resolvable when replaying the run.
                "elements": [e.model_dump(mode="json") for e in observation.elements],
            },
        )

        if observation.page_kind.value == "application_form" and handle.state is S.BROWSER_RUNNING:
            await self._transition(handle, S.FORM_ANALYZED)

        return observation

    async def _step(
        self, handle: RunHandle, observation: Observation, decision: Decision
    ) -> bool:
        """One Policy -> (approval) -> Execute cycle. Returns True if the run is over."""
        verdict = self._policy.evaluate(decision, observation, handle.policy)
        await self._bus.emit(
            handle.run_id,
            EventType.POLICY_VERDICT,
            f"{verdict.outcome.value} [{verdict.rule_id}] {verdict.reason}",
            agent="policy",
            payload=verdict.model_dump(mode="json"),
        )

        if verdict.outcome is PolicyOutcome.DENY:
            # Mark the field dealt with, or the reasoner proposes it again forever.
            self._mark_handled(handle, observation, decision)
            return False

        if decision.action is ActionType.SUBMIT:
            # Surfaced *before* the approval card, so you can see what was left blank while
            # deciding whether to submit -- not after you have already said yes.
            await self._warn_unfilled_required(handle, observation)

        if verdict.outcome is PolicyOutcome.REQUIRE_APPROVAL:
            approved, decision = await self._await_approval(
                handle, observation, decision, verdict
            )
            if not approved:
                self._mark_handled(handle, observation, decision)
                await self._transition(handle, S.SAFE_FIELDS_FILLED, tolerant=True)
                return False
            handle.policy.grant(decision)
            verdict = self._policy.evaluate(decision, observation, handle.policy)
            if verdict.blocks_execution:
                await self._bus.emit(
                    handle.run_id,
                    EventType.POLICY_VERDICT,
                    f"Still blocked after approval [{verdict.rule_id}] {verdict.reason}",
                    agent="policy",
                    payload=verdict.model_dump(mode="json"),
                )
                self._mark_handled(handle, observation, decision)
                return False

        if decision.action is ActionType.SUBMIT:
            await self._transition(handle, S.SUBMITTING, tolerant=True)

        assert handle.session is not None
        result = await self._executor.execute(decision, observation, handle.session)
        handle.policy.actions_executed += 1
        handle.policy.executed_signatures.add(action_signature(decision, observation))

        await self._record_action(handle, observation, decision, verdict.rule_id, result)
        await self._bus.emit(
            handle.run_id,
            EventType.ACTION_RESULT,
            ("simulated " if result.simulated else "")
            + f"{decision.action.value} "
            + ("ok" if result.success else f"failed: {result.error}"),
            agent="executor",
            payload=result.model_dump(mode="json"),
        )

        self._mark_handled(handle, observation, decision)

        if decision.action is ActionType.SUBMIT and result.success:
            await self._transition(handle, S.SUBMITTED)
            await self._mark_submitted(handle, simulated=result.simulated)
            await self._bus.emit(
                handle.run_id,
                EventType.RUN_FINISHED,
                "Application submitted (simulated — DRY_RUN is on)."
                if result.simulated
                else "Application submitted.",
            )
            await self._finish(handle, "finished", "submitted")
            return True

        return False

    def _help_would_repeat(self, handle: RunHandle, observation: Observation) -> str | None:
        """Record this request for help, and say why the run should stop if it is going in
        circles. `None` means carry on and ask.

        The agent asking for help is normal -- a CAPTCHA, a login wall, a question only the
        person can answer. Asking the *same* question about the *same* unchanged page after
        the person has already responded is not: nothing the human did moved the run, so
        asking again will not either. Failing with a reason beats a loop the user has to
        notice and kill by hand.
        """
        page = help_page_key(observation)
        if page == handle.last_help_page:
            handle.help_at_this_page += 1
        else:
            handle.last_help_page = page
            handle.help_at_this_page = 0
        handle.help_requests += 1

        if handle.help_at_this_page >= MAX_HELP_AT_ONE_PAGE:
            return (
                f"Stopping: the agent has asked for help {handle.help_at_this_page + 1} times "
                f"about the same page and nothing on it changed in between. It cannot get "
                f"past this step on its own. Finish this application in the browser, or "
                f"start a new run once the page has moved on."
            )
        if handle.help_requests > MAX_HELP_REQUESTS:
            return (
                f"Stopping: the agent has asked for help {handle.help_requests} times in one "
                f"run, which is more than this step should ever need. Something on this site "
                f"is not working the way the agent expects."
            )
        return None

    async def _ask_for_help(
        self, handle: RunHandle, observation: Observation, decision: Decision
    ) -> bool:
        """Surface an ASK_USER as an approval card. True to carry on, False to stop."""
        approval = m.Approval(
            run_id=handle.run_id,
            fingerprint=decision_fingerprint(decision),
            action=ActionType.ASK_USER.value,
            target_ref=decision.target_ref,
            target_name=observation.title or observation.url,
            proposed_value=None,
            reason=decision.reason,
            policy_rule="R000_AGENT_ASKED",
        )
        await self._save(approval)

        pending = PendingApproval(approval_id=approval.id, decision=decision)
        handle.pending = pending
        handle.status = "waiting_approval"

        await self._bus.emit(
            handle.run_id,
            EventType.APPROVAL_REQUESTED,
            f"The agent needs you: {decision.reason}",
            agent=self._reasoner.name,
            payload={
                "approval_id": str(approval.id),
                "action": ActionType.ASK_USER.value,
                "target_name": approval.target_name,
                "reason": decision.reason,
                "policy_rule": "R000_AGENT_ASKED",
                "help": (
                    "Do whatever is needed in the browser window, then approve to carry on. "
                    "Reject to end the run."
                ),
            },
        )

        await pending.event.wait()
        handle.pending = None
        handle.status = "running"

        # Whatever the person did in the browser invalidates what the agent last saw, so
        # every ref is dropped and the loop re-observes before acting again.
        if pending.approved and handle.session is not None:
            handle.session.invalidate()
        return pending.approved

    async def _await_approval(
        self, handle: RunHandle, observation: Observation, decision: Decision, verdict
    ) -> tuple[bool, Decision]:
        element = observation.element(decision.target_ref) if decision.target_ref else None
        approval = m.Approval(
            run_id=handle.run_id,
            fingerprint="",  # filled below, once the final decision is known
            action=decision.action.value,
            target_ref=decision.target_ref,
            target_name=element.name if element else None,
            proposed_value=decision.value,
            reason=decision.reason,
            policy_rule=verdict.rule_id,
            field_class=verdict.field_class,
        )
        approval.fingerprint = decision_fingerprint(decision)
        await self._save(approval)

        # State first, then expose the pending approval: a client that sees `pending` set must
        # already see REVIEW_REQUIRED, never an in-between.
        handle.status = "waiting_approval"
        await self._transition(handle, S.SAFE_FIELDS_FILLED, tolerant=True)
        await self._transition(handle, S.REVIEW_REQUIRED, tolerant=True)

        pending = PendingApproval(approval_id=approval.id, decision=decision)
        handle.pending = pending

        await self._bus.emit(
            handle.run_id,
            EventType.APPROVAL_REQUESTED,
            f"Needs your approval: {verdict.reason}",
            agent="policy",
            payload={
                "approval_id": str(approval.id),
                "action": decision.action.value,
                "target_ref": decision.target_ref,
                "target_name": approval.target_name,
                "proposed_value": decision.value,
                "reason": decision.reason,
                "policy_rule": verdict.rule_id,
                "field_class": verdict.field_class,
            },
        )

        await pending.event.wait()
        handle.pending = None
        handle.status = "running"

        if not pending.approved:
            return False, decision

        if pending.edited_value is not None and pending.edited_value != decision.value:
            # An edited value is a different action, so it gets its own fingerprint.
            decision = decision.model_copy(update={"value": pending.edited_value})
            await self._update_approval(
                approval.id, fingerprint=decision_fingerprint(decision)
            )
        return True, decision

    # -- bookkeeping --------------------------------------------------------------------

    def _mark_handled(
        self, handle: RunHandle, observation: Observation, decision: Decision
    ) -> None:
        if decision.target_ref is None:
            return
        element = observation.element(decision.target_ref)
        if element is not None and element.name:
            handle.reasoning.handled_fields.add(normalise(element.name))

    async def _warn_unfilled_required(
        self, handle: RunHandle, observation: Observation
    ) -> None:
        """Fields the agent deliberately would not touch may still be required. Say so rather
        than letting the submit fail with a mystery validation error."""
        outstanding = [
            e.name
            for e in observation.elements
            if e.required
            and e.visible
            and not (e.value or "").strip()
            and classify(e).field_class is FieldClass.NEVER_AUTOFILL
        ]
        if outstanding:
            await self._bus.emit(
                handle.run_id,
                EventType.LOG,
                "Left for you to complete by hand: " + ", ".join(outstanding),
                agent="policy",
                payload={"fields": outstanding},
            )

    @staticmethod
    def _redact(observation: Observation, decision: Decision) -> str | None:
        """Never persist a value destined for a NEVER_AUTOFILL field."""
        if decision.value is None or decision.target_ref is None:
            return decision.value
        element = observation.element(decision.target_ref)
        if element is not None and classify(element).field_class is FieldClass.NEVER_AUTOFILL:
            return "***redacted***"
        return decision.value

    async def _record_action(
        self,
        handle: RunHandle,
        observation: Observation,
        decision: Decision,
        rule_id: str,
        result: ActionResult,
    ) -> None:
        element = observation.element(decision.target_ref) if decision.target_ref else None
        await self._save(
            m.BrowserAction(
                run_id=handle.run_id,
                session_id=handle.session.session_id if handle.session else None,
                action=decision.action.value,
                target_ref=decision.target_ref,
                target_name=element.name if element else None,
                value=self._redact(observation, decision),
                success=result.success,
                simulated=result.simulated,
                error=result.error,
                duration_ms=result.duration_ms,
                policy_rule=rule_id,
            )
        )

    async def _transition(
        self, handle: RunHandle, target: ApplicationState, *, tolerant: bool = False
    ) -> None:
        if handle.state is target:
            return
        try:
            handle.state = transition(handle.state, target)
        except InvalidTransition:
            if tolerant:
                return
            raise
        await self._bus.emit(
            handle.run_id,
            EventType.STATE_CHANGED,
            handle.state.value,
            payload={"state": handle.state.value},
        )
        if handle.application_id is not None:
            await self._update_application(handle.application_id, state=handle.state.value)

    async def _finish(self, handle: RunHandle, status: str, reason: str) -> None:
        handle.status = status
        await self._close_session(handle)
        await self._update_run(
            handle.run_id,
            status=status,
            error=reason if status in {"failed", "stopped"} else None,
            actions_executed=handle.policy.actions_executed,
            finished_at=datetime.now(UTC),
        )

    async def _close_session(self, handle: RunHandle) -> None:
        if handle.session is not None:
            await self._browser.close_session(handle.session.session_id)
            handle.session = None

    # -- persistence --------------------------------------------------------------------

    async def _save(self, *rows) -> None:
        async with session_factory()() as session:
            for row in rows:
                session.add(row)
            await session.commit()

    async def _update_run(self, run_id: UUID, **values) -> None:
        async with session_factory()() as session:
            run = await session.get(m.AgentRun, run_id)
            if run is None:
                return
            for key, value in values.items():
                setattr(run, key, value)
            session.add(run)
            await session.commit()

    async def _update_application(self, application_id: UUID, **values) -> None:
        async with session_factory()() as session:
            application = await session.get(m.Application, application_id)
            if application is None:
                return
            for key, value in values.items():
                setattr(application, key, value)
            application.updated_at = datetime.now(UTC)
            session.add(application)
            await session.commit()

    async def _mark_submitted(self, handle: RunHandle, *, simulated: bool) -> None:
        if handle.application_id is None:
            return
        await self._update_application(
            handle.application_id,
            submitted_at=datetime.now(UTC),
            simulated=simulated,
        )

    async def _update_approval(self, approval_id: UUID, **values) -> None:
        async with session_factory()() as session:
            approval = await session.get(m.Approval, approval_id)
            if approval is None:
                return
            for key, value in values.items():
                setattr(approval, key, value)
            if "status" in values:
                approval.resolved_at = datetime.now(UTC)
            session.add(approval)
            await session.commit()

    async def pending_approvals(self) -> list[m.Approval]:
        async with session_factory()() as session:
            result = await session.execute(
                select(m.Approval).where(m.Approval.status == "pending")
            )
            return list(result.scalars().all())
