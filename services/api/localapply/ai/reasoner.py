"""The Reason layer: `Observation` -> `Decision`.

A reasoner is a **pure function of its inputs**. It receives an observation and a context and
returns a proposal. It holds no browser handle, no database session, and no network access of
its own. It cannot act; it can only suggest, and the policy engine decides.

`StubReasoner` is scripted and deterministic. It exists so the entire loop -- observer,
policy, executor, events, approvals, kill switch -- can be built and tested end to end before
a single model weight is downloaded, and so every test in the suite has a fixed expected
outcome. `LLMReasoner` implements the same interface for Phase 4.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..contracts import (
    TARGETED_ACTIONS,
    ActionType,
    Decision,
    ElementRole,
    Observation,
    ObservedElement,
    PageKind,
)
from ..policy.field_classifier import Classification, FieldClass, classify
from .prompting import (
    REASONER_SYSTEM_PROMPT,
    render_action_menu,
    render_element_table,
    render_profile,
    wrap_untrusted,
)

_APPLY_RE = re.compile(r"\b(easy\s*)?apply\b|apply now|start (your )?application", re.IGNORECASE)
_SUBMIT_RE = re.compile(
    r"\bsubmit\b|send application|finish application|complete application", re.IGNORECASE
)

#: Elements the reasoner may try to fill.
_FILLABLE: frozenset[ElementRole] = frozenset(
    {
        ElementRole.TEXTBOX,
        ElementRole.TEXTAREA,
        ElementRole.COMBOBOX,
        ElementRole.FILE_INPUT,
        ElementRole.CHECKBOX,
    }
)


def normalise(name: str) -> str:
    """Field identity that survives re-observation. Refs change every observation; names do
    not, so progress is tracked by name."""
    return re.sub(r"\s+", " ", name).strip().lower()


@dataclass
class ReasoningContext:
    """What the reasoner knows. Owned and mutated by the run loop, never by the reasoner."""

    goal: str = "Complete this job application."
    #: Verified profile facts, keyed by the profile keys in field_classifier.SAFE_PATTERNS.
    profile: dict[str, str] = field(default_factory=dict)
    #: Pre-drafted answers for REVIEW_REQUIRED fields, keyed by the classifier's match label
    #: (e.g. "salary"). These are proposals the human confirms; they are never entered blind.
    drafts: dict[str, str] = field(default_factory=dict)
    #: Normalised field names already dealt with -- filled, denied, or deliberately skipped.
    #: Prevents the loop from re-proposing a field that policy just rejected.
    handled_fields: set[str] = field(default_factory=set)
    job_title: str | None = None
    company: str | None = None


class Reasoner(ABC):
    name: str = "reasoner"

    @abstractmethod
    async def decide(self, observation: Observation, context: ReasoningContext) -> Decision: ...


class StubReasoner(Reasoner):
    """Deterministic, rule-driven. No model, no randomness, no network."""

    name = "stub"

    async def decide(self, observation: Observation, context: ReasoningContext) -> Decision:
        kind = observation.page_kind

        if kind is PageKind.CONFIRMATION:
            return Decision(
                action=ActionType.FINISH,
                confidence=0.99,
                reason="Page confirms the application was received.",
            )

        if kind in {PageKind.CAPTCHA, PageKind.LOGIN}:
            return Decision(
                action=ActionType.ASK_USER,
                confidence=0.99,
                reason=(
                    f"This is a {kind.value} page. Anti-bot checks and logins are handed to you "
                    "rather than worked around."
                ),
            )

        if kind is PageKind.ERROR:
            return Decision(
                action=ActionType.WAIT,
                value="1000",
                confidence=0.5,
                reason="Page was unreadable; waiting for it to settle.",
            )

        if kind is PageKind.APPLICATION_FORM:
            return self._work_the_form(observation, context)

        apply_button = self._find(observation, _APPLY_RE, {ElementRole.BUTTON, ElementRole.LINK})
        if apply_button is not None and normalise(apply_button.name) not in context.handled_fields:
            return Decision(
                action=ActionType.CLICK,
                target_ref=apply_button.ref,
                confidence=0.93,
                reason=f"{apply_button.name!r} starts the application.",
            )

        return Decision(
            action=ActionType.FINISH,
            confidence=0.7,
            reason="Nothing further to do on this page.",
        )

    # -- form handling ------------------------------------------------------------------

    def _work_the_form(self, observation: Observation, context: ReasoningContext) -> Decision:
        for element in observation.elements:
            if element.role not in _FILLABLE or not element.visible or not element.enabled:
                continue
            key = normalise(element.name)
            if not key or key in context.handled_fields:
                continue

            result = classify(element)

            # Signatures, demographics, government IDs: the agent does not touch these, and
            # does not propose touching them. They are left for the human at review time.
            if result.field_class is FieldClass.NEVER_AUTOFILL:
                continue

            decision = self._fill(element, result, context)
            if decision is not None:
                return decision

        submit = self._find(observation, _SUBMIT_RE, {ElementRole.BUTTON})
        if submit is not None and normalise(submit.name) not in context.handled_fields:
            return Decision(
                action=ActionType.SUBMIT,
                target_ref=submit.ref,
                confidence=0.90,
                reason="Every field the agent can fill is complete; requesting approval to submit.",
            )

        # A submit that was already attempted -- or that you rejected -- must not be proposed
        # again, or the run would ask forever.
        return Decision(
            action=ActionType.FINISH,
            confidence=0.6,
            reason=(
                "Submit was declined; stopping."
                if submit is not None
                else "Form is filled but no submit control was found."
            ),
        )

    def _fill(
        self, element: ObservedElement, result: Classification, context: ReasoningContext
    ) -> Decision | None:
        if result.field_class is FieldClass.SAFE_AUTOFILL:
            value = context.profile.get(result.profile_key or "")
            confidence = 0.95
            why = f"{result.matched} maps to a verified profile fact."
        else:
            # REVIEW_REQUIRED: a drafted answer, which policy rule R011 will route to you.
            value = context.drafts.get(result.matched)
            confidence = 0.72
            why = f"{result.matched} is a drafted answer and needs your confirmation."

        if value is None:
            return None

        if element.role is ElementRole.FILE_INPUT:
            action = ActionType.UPLOAD
        elif element.role is ElementRole.COMBOBOX:
            action = ActionType.SELECT
        elif element.role is ElementRole.CHECKBOX:
            action = ActionType.CLICK
            value = None
        else:
            action = ActionType.TYPE

        return Decision(
            action=action,
            target_ref=element.ref,
            value=value,
            confidence=confidence,
            reason=f"Field {element.name!r}: {why}",
        )

    @staticmethod
    def _find(
        observation: Observation, pattern: re.Pattern[str], roles: set[ElementRole]
    ) -> ObservedElement | None:
        for element in observation.elements:
            if (
                element.role in roles
                and element.visible
                and element.enabled
                and pattern.search(element.name)
            ):
                return element
        return None


class LLMReasoner(Reasoner):
    """Phase 4. Same contract, a model behind it.

    Wired but not exercised in the walking skeleton: `LA_REASONER=stub` is the default. The
    prompt is built here so the untrusted-content boundary is visible in one place.
    """

    name = "llm"

    def __init__(self, router) -> None:
        self._router = router

    def build_prompt(self, observation: Observation, context: ReasoningContext) -> str:
        # The URL and the title are written by whoever controls the page. Left bare they sit
        # directly beside GOAL and ELEMENTS, which is the best position on the whole prompt
        # for injected text -- and a <title> has no length limit, so it can carry a closing
        # fence marker and a paragraph of instructions. `page_kind` is ours: the observer
        # decided it, in code.
        parts = [
            f"GOAL: {context.goal}",
            f"PAGE KIND: {observation.page_kind.value}",
            "PAGE URL AND TITLE:",
            wrap_untrusted(f"{observation.url}\n{observation.title}", limit=600),
            "ELEMENTS (address these by ref, and only these):",
            render_element_table(observation.elements, context.handled_fields),
        ]

        # Without this the model has to invent values, and it does -- "JohnDoe" typed into a
        # real First name field. Give it the actual facts, and forbid anything else.
        if context.profile or context.drafts:
            parts += ["CANDIDATE DETAILS (use these values verbatim; invent nothing):",
                      render_profile(context.profile, context.drafts)]
        else:
            parts.append(
                "CANDIDATE DETAILS: none available. Do not invent any value; "
                "choose ask_user if a field needs one."
            )

        parts += [
            "PAGE TEXT:",
            wrap_untrusted(observation.untrusted_text),
            render_action_menu(),
        ]
        return "\n\n".join(parts)

    #: One corrective retry. A small local model quite often returns prose around the JSON
    #: on its first attempt and gets it right when told exactly what was wrong -- cheaper
    #: than surfacing an ASK_USER and stalling the run for a formatting slip.
    MAX_ATTEMPTS = 2

    async def decide(self, observation: Observation, context: ReasoningContext) -> Decision:
        prompt = self.build_prompt(observation, context)
        last_error = "no attempt was made"

        for _attempt in range(self.MAX_ATTEMPTS):
            try:
                raw = await self._router.generate(prompt, system=REASONER_SYSTEM_PROMPT)
            except Exception as exc:  # noqa: BLE001 - a dead model must not kill the run
                return Decision(
                    action=ActionType.ASK_USER,
                    confidence=0.0,
                    reason=f"The model could not be reached: {exc.__class__.__name__}. "
                           "Check that Ollama is running.",
                )

            decision = self.parse(raw, observation)
            if decision.action is not ActionType.ASK_USER or decision.confidence > 0:
                return decision

            last_error = decision.reason
            prompt = (
                f"{self.build_prompt(observation, context)}\n\n"
                f"Your previous reply was rejected: {last_error}\n"
                "Reply with a single JSON object and nothing else. "
                "target_ref must be one of the refs in the element table above."
            )

        return Decision(
            action=ActionType.ASK_USER,
            confidence=0.0,
            reason=f"The model did not return a usable action after "
                   f"{self.MAX_ATTEMPTS} attempts. Last problem: {last_error}",
        )

    @staticmethod
    def parse(raw: str, observation: Observation) -> Decision:
        """Parse a model reply into a Decision, failing safe.

        A model that returns junk, or names an element that does not exist, produces an
        ASK_USER rather than an exception or a guess. Policy rule R002 would also catch an
        unknown ref; catching it here keeps the reason message useful.
        """
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match is None:
            return Decision(
                action=ActionType.ASK_USER, confidence=0.0, reason="Model reply was not JSON."
            )
        try:
            data = json.loads(match.group(0))
            decision = Decision.model_validate(data)
        except Exception as exc:
            return Decision(
                action=ActionType.ASK_USER,
                confidence=0.0,
                reason=f"Model reply could not be parsed: {exc.__class__.__name__}",
            )

        if decision.action in TARGETED_ACTIONS and decision.target_ref is None:
            # Policy denies this too (R002_MISSING_TARGET), but denial ends the step while
            # this is a formatting slip the retry loop can fix. It is also what a model
            # answering with coordinates produces: `{"action": "click", "x": 450, "y": 320}`
            # validates, because the unknown keys are simply dropped -- leaving a click with
            # nothing to click.
            return Decision(
                action=ActionType.ASK_USER,
                confidence=0.0,
                reason=f"{decision.action.value} needs a target_ref from the element table. "
                       "A position on the screen is not something this can act on.",
            )

        if decision.target_ref is not None and decision.target_ref not in observation.refs():
            return Decision(
                action=ActionType.ASK_USER,
                confidence=0.0,
                reason=f"Model named element {decision.target_ref!r}, which was not observed.",
            )
        return decision
