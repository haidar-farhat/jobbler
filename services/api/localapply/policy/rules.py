"""Policy rules and the run context they evaluate against.

A rule is `(Decision, Observation, RunContext) -> PolicyVerdict | None`, where `None` means
"this rule has no opinion". Rules are pure functions of their arguments: no I/O, no model
calls, no clock. That is what makes them exhaustively unit-testable.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from ..contracts import (
    MUTATING_ACTIONS,
    TARGETED_ACTIONS,
    ActionType,
    Decision,
    Observation,
    PageKind,
    PolicyOutcome,
    PolicyVerdict,
)
from .field_classifier import FieldClass, classify


def decision_fingerprint(decision: Decision) -> str:
    """Stable identity for a decision, so a human approval can be tied to exactly one action.

    Approving "type 90000 into e12" must not also approve "type 90000 into e13".
    """
    raw = f"{decision.action.value}|{decision.target_ref or ''}|{decision.value or ''}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass
class RunContext:
    """Everything policy needs to know about the run, and nothing about the page."""

    agent: str
    capabilities: frozenset[ActionType]
    actions_executed: int = 0
    max_actions: int = 120
    min_confidence: float = 0.60
    kill_switch: bool = False
    dry_run: bool = True
    #: Fingerprints a human has explicitly approved this run.
    granted_approvals: set[str] = field(default_factory=set)

    def grant(self, decision: Decision) -> None:
        self.granted_approvals.add(decision_fingerprint(decision))

    def is_granted(self, decision: Decision) -> bool:
        return decision_fingerprint(decision) in self.granted_approvals


def _deny(rule_id: str, reason: str, field_class: str | None = None) -> PolicyVerdict:
    return PolicyVerdict(
        outcome=PolicyOutcome.DENY, rule_id=rule_id, reason=reason, field_class=field_class
    )


def _gate(rule_id: str, reason: str, field_class: str | None = None) -> PolicyVerdict:
    return PolicyVerdict(
        outcome=PolicyOutcome.REQUIRE_APPROVAL,
        rule_id=rule_id,
        reason=reason,
        field_class=field_class,
    )


# ======================================================================================
# DENY rules -- evaluated first, and they outrank human approval. A human cannot approve
# their way past these.
# ======================================================================================


def r001_kill_switch(d: Decision, o: Observation, c: RunContext) -> PolicyVerdict | None:
    if c.kill_switch:
        return _deny("R001_KILL_SWITCH", "All automation is stopped by the kill switch.")
    return None


def r002_unknown_ref(d: Decision, o: Observation, c: RunContext) -> PolicyVerdict | None:
    """The core defence from ADR 0002: a decision may only name an element the observer
    actually enumerated *in this observation*. Stale and invented refs both fail here."""
    if d.action not in TARGETED_ACTIONS:
        return None
    if d.target_ref is None:
        return _deny("R002_MISSING_TARGET", f"{d.action.value} requires a target element.")
    if d.target_ref not in o.refs():
        return _deny(
            "R002_UNKNOWN_REF",
            f"Element {d.target_ref!r} is not in the current observation. "
            "It is stale, or was never observed.",
        )
    return None


def r003_capability(d: Decision, o: Observation, c: RunContext) -> PolicyVerdict | None:
    if d.action not in c.capabilities:
        return _deny(
            "R003_CAPABILITY",
            f"Agent {c.agent!r} has no capability for action {d.action.value!r}.",
        )
    return None


def r004_action_budget(d: Decision, o: Observation, c: RunContext) -> PolicyVerdict | None:
    if c.actions_executed >= c.max_actions:
        return _deny(
            "R004_ACTION_BUDGET",
            f"Run exceeded its budget of {c.max_actions} actions.",
        )
    return None


def r005_never_autofill(d: Decision, o: Observation, c: RunContext) -> PolicyVerdict | None:
    """Signatures, government IDs, demographics, credentials. Not fillable at any confidence,
    and not unlockable by approval -- the human fills these themselves."""
    if d.action not in {ActionType.TYPE, ActionType.SELECT, ActionType.UPLOAD}:
        return None
    element = o.element(d.target_ref) if d.target_ref else None
    if element is None:
        return None
    result = classify(element)
    if result.field_class is FieldClass.NEVER_AUTOFILL:
        return _deny(
            "R005_NEVER_AUTOFILL",
            f"Field {element.name!r} is {result.matched}; it must be completed by hand.",
            field_class=result.field_class.value,
        )
    return None


def r006_element_unusable(d: Decision, o: Observation, c: RunContext) -> PolicyVerdict | None:
    if d.action not in TARGETED_ACTIONS or d.target_ref is None:
        return None
    element = o.element(d.target_ref)
    if element is None:
        return None
    if not element.enabled:
        return _deny("R006_ELEMENT_DISABLED", f"Element {element.name!r} is disabled.")
    if not element.visible:
        return _deny("R006_ELEMENT_HIDDEN", f"Element {element.name!r} is not visible.")
    return None


DENY_RULES = [
    r001_kill_switch,
    r002_unknown_ref,
    r003_capability,
    r004_action_budget,
    r005_never_autofill,
    r006_element_unusable,
]


# ======================================================================================
# APPROVAL rules -- a human may unlock these.
# ======================================================================================


def r010_submit_always_gated(d: Decision, o: Observation, c: RunContext) -> PolicyVerdict | None:
    """There is exactly one path to submitting an application, and it goes through a human.
    No confidence score and no model output can bypass this."""
    if d.action is ActionType.SUBMIT:
        return _gate("R010_SUBMIT_ALWAYS_GATED", "Submitting an application always needs approval.")
    return None


def r011_review_required_field(d: Decision, o: Observation, c: RunContext) -> PolicyVerdict | None:
    if d.action not in {ActionType.TYPE, ActionType.SELECT, ActionType.UPLOAD}:
        return None
    element = o.element(d.target_ref) if d.target_ref else None
    if element is None:
        return None
    result = classify(element)
    if result.field_class is FieldClass.REVIEW_REQUIRED:
        return _gate(
            "R011_REVIEW_REQUIRED_FIELD",
            f"Field {element.name!r} is {result.matched}; confirm the value before it is entered.",
            field_class=result.field_class.value,
        )
    return None


def r012_low_confidence(d: Decision, o: Observation, c: RunContext) -> PolicyVerdict | None:
    if d.action in MUTATING_ACTIONS and d.confidence < c.min_confidence:
        return _gate(
            "R012_LOW_CONFIDENCE",
            f"Confidence {d.confidence:.2f} is below the {c.min_confidence:.2f} threshold.",
        )
    return None


def r013_intervention_page(d: Decision, o: Observation, c: RunContext) -> PolicyVerdict | None:
    """CAPTCHAs and login walls are handed to the human, never solved or worked around.
    That is both the honest engineering choice and the correct one."""
    if o.page_kind in {PageKind.CAPTCHA, PageKind.LOGIN} and d.action in MUTATING_ACTIONS:
        return _gate(
            "R013_INTERVENTION_PAGE",
            f"Page looks like a {o.page_kind.value}; it needs you, not the agent.",
        )
    return None


APPROVAL_RULES = [
    r010_submit_always_gated,
    r011_review_required_field,
    r012_low_confidence,
    r013_intervention_page,
]
