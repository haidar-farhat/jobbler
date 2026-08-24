"""The Execute layer: an allowed `Decision` -> a real browser action.

Entirely deterministic. No model is involved in deciding *how* to click a button. The
executor interprets nothing: it resolves a ref, performs a mechanical operation, and reports
what happened.

Two guarantees live here, both defence-in-depth behind the policy engine:

  * the kill switch is checked before **every** action;
  * an unresolvable ref never reaches Playwright.

Policy already enforces both. The executor enforces them again because a bug in the layer
above should not be able to produce a click.
"""

from __future__ import annotations

import time

from playwright.async_api import Error as PlaywrightError

from ..config import Settings
from ..contracts import TARGETED_ACTIONS, ActionResult, ActionType, Decision, Observation
from ..safety import KILL_SWITCH, AutomationHalted
from .session import BrowserSession


class BrowserExecutor:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def execute(
        self, decision: Decision, observation: Observation, session: BrowserSession
    ) -> ActionResult:
        if KILL_SWITCH.engaged:
            raise AutomationHalted(KILL_SWITCH.reason or "Automation stopped.")

        started = time.perf_counter()

        def done(
            success: bool, error: str | None = None, simulated: bool = False
        ) -> ActionResult:
            return ActionResult(
                action=decision.action,
                success=success,
                new_page_state=session.page.url,
                error=error,
                duration_ms=int((time.perf_counter() - started) * 1000),
                simulated=simulated,
            )

        locator = None
        if decision.action in TARGETED_ACTIONS:
            if decision.target_ref is None:
                return done(False, f"{decision.action.value} requires a target ref.")
            locator = session.resolve(decision.target_ref)
            if locator is None:
                # Should be unreachable: policy rule R002 rejects this first.
                return done(
                    False,
                    f"Ref {decision.target_ref!r} is not resolvable in the current session.",
                )

        try:
            return await self._dispatch(decision, session, locator, done)
        except PlaywrightError as exc:
            return done(False, f"{exc.__class__.__name__}: {str(exc).splitlines()[0][:300]}")

    async def _dispatch(self, decision, session, locator, done):
        page = session.page
        action = decision.action
        value = decision.value

        if action is ActionType.NAVIGATE:
            if not value:
                return done(False, "NAVIGATE requires a URL in `value`.")
            await page.goto(value, wait_until="domcontentloaded")
            # Every ref described the previous page. None survive a navigation.
            session.invalidate()
            return done(True)

        if action is ActionType.CLICK:
            await locator.click()
            return done(True)

        if action is ActionType.TYPE:
            await locator.fill(value or "")
            return done(True)

        if action is ActionType.SELECT:
            if not value:
                return done(False, "SELECT requires an option in `value`.")
            await locator.select_option(label=value)
            return done(True)

        if action is ActionType.UPLOAD:
            if not value:
                return done(False, "UPLOAD requires a file path in `value`.")
            await locator.set_input_files(value)
            return done(True)

        if action is ActionType.SCROLL:
            await page.mouse.wheel(0, 800)
            return done(True)

        if action is ActionType.WAIT:
            await page.wait_for_timeout(min(int(value or 1000), 10_000))
            return done(True)

        if action in {ActionType.EXTRACT, ActionType.SCREENSHOT}:
            # Reading is the observer's job; these exist so a reasoner can ask for a fresh
            # look without mutating anything.
            return done(True)

        if action is ActionType.SUBMIT:
            if self._settings.dry_run:
                # The whole point of DRY_RUN: the decision, the approval, and the log entry
                # are all real. Only the click is withheld.
                return done(True, simulated=True)
            await locator.click()
            await page.wait_for_load_state("domcontentloaded")
            session.invalidate()
            return done(True)

        if action in {ActionType.FINISH, ActionType.ASK_USER}:
            # Control-flow actions; the run loop handles these before reaching the executor.
            return done(True)

        return done(False, f"Unsupported action {action.value!r}.")
