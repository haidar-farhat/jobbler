"""The policy engine. Pure functions, so these are exhaustive and fast."""

from __future__ import annotations

from localapply.contracts import (
    ActionType,
    Decision,
    ElementRole,
    PageKind,
    PolicyOutcome,
)
from localapply.policy.capabilities import capabilities_for
from localapply.policy.rules import RunContext
from localapply.safety import KILL_SWITCH


def _type(ref="e1", value="x", confidence=0.95):
    return Decision(
        action=ActionType.TYPE, target_ref=ref, value=value, confidence=confidence, reason="test"
    )


# --------------------------------------------------------------------------------------
# DENY
# --------------------------------------------------------------------------------------


def test_kill_switch_denies_everything(engine, make_element, make_observation, application_context):
    observation = make_observation([make_element()])
    KILL_SWITCH.engage("test")
    verdict = engine.evaluate(_type(), observation, application_context)
    assert verdict.outcome is PolicyOutcome.DENY
    assert verdict.rule_id == "R001_KILL_SWITCH"


def test_unknown_ref_is_denied(engine, make_element, make_observation, application_context):
    """ADR 0002's core guarantee: a ref the observer did not produce cannot be acted on."""
    observation = make_observation([make_element(ref="e1")])
    verdict = engine.evaluate(_type(ref="e999"), observation, application_context)
    assert verdict.outcome is PolicyOutcome.DENY
    assert verdict.rule_id == "R002_UNKNOWN_REF"


def test_stale_ref_from_a_previous_page_is_denied(
    engine, make_element, make_observation, application_context
):
    """Refs are rebuilt per observation, so yesterday's e3 is not today's e3."""
    fresh_page = make_observation([make_element(ref="e1"), make_element(ref="e2")])
    verdict = engine.evaluate(_type(ref="e3"), fresh_page, application_context)
    assert verdict.outcome is PolicyOutcome.DENY


def test_targeted_action_without_a_ref_is_denied(engine, make_observation, application_context):
    decision = Decision(action=ActionType.CLICK, confidence=0.9, reason="test")
    verdict = engine.evaluate(decision, make_observation(), application_context)
    assert verdict.outcome is PolicyOutcome.DENY
    assert verdict.rule_id == "R002_MISSING_TARGET"


def test_agent_without_capability_is_denied(engine, make_element, make_observation):
    """The discovery agent reads job listings. It cannot type into a form, by construction."""
    discovery = RunContext(agent="discovery", capabilities=capabilities_for("discovery"))
    observation = make_observation([make_element()])
    verdict = engine.evaluate(_type(), observation, discovery)
    assert verdict.outcome is PolicyOutcome.DENY
    assert verdict.rule_id == "R003_CAPABILITY"


def test_unknown_agent_gets_no_capabilities(engine, make_element, make_observation):
    rogue = RunContext(agent="not-a-real-agent", capabilities=capabilities_for("not-a-real-agent"))
    verdict = engine.evaluate(_type(), make_observation([make_element()]), rogue)
    assert verdict.outcome is PolicyOutcome.DENY


def test_action_budget_is_enforced(engine, make_element, make_observation, application_context):
    application_context.actions_executed = application_context.max_actions
    verdict = engine.evaluate(_type(), make_observation([make_element()]), application_context)
    assert verdict.outcome is PolicyOutcome.DENY
    assert verdict.rule_id == "R004_ACTION_BUDGET"


def test_never_autofill_field_is_denied(
    engine, make_element, make_observation, application_context
):
    element = make_element(name="Electronic signature")
    verdict = engine.evaluate(_type(), make_observation([element]), application_context)
    assert verdict.outcome is PolicyOutcome.DENY
    assert verdict.rule_id == "R005_NEVER_AUTOFILL"


def test_disabled_element_is_denied(engine, make_element, make_observation, application_context):
    element = make_element(enabled=False)
    verdict = engine.evaluate(_type(), make_observation([element]), application_context)
    assert verdict.outcome is PolicyOutcome.DENY


# --------------------------------------------------------------------------------------
# REQUIRE_APPROVAL
# --------------------------------------------------------------------------------------


def test_submit_always_requires_approval(
    engine, make_element, make_observation, application_context
):
    """No confidence score, model output, or page content can bypass this."""
    button = make_element(ref="e1", name="Submit application", role=ElementRole.BUTTON)
    decision = Decision(
        action=ActionType.SUBMIT, target_ref="e1", confidence=1.0, reason="all fields done"
    )
    verdict = engine.evaluate(decision, make_observation([button]), application_context)
    assert verdict.outcome is PolicyOutcome.REQUIRE_APPROVAL
    assert verdict.rule_id == "R010_SUBMIT_ALWAYS_GATED"


def test_review_required_field_is_gated(
    engine, make_element, make_observation, application_context
):
    element = make_element(name="Expected salary")
    verdict = engine.evaluate(_type(value="90000"), make_observation([element]), application_context)
    assert verdict.outcome is PolicyOutcome.REQUIRE_APPROVAL
    assert verdict.rule_id == "R011_REVIEW_REQUIRED_FIELD"
    assert verdict.field_class == "review_required"


def test_low_confidence_mutation_is_gated(
    engine, make_element, make_observation, application_context
):
    verdict = engine.evaluate(
        _type(confidence=0.2), make_observation([make_element()]), application_context
    )
    assert verdict.outcome is PolicyOutcome.REQUIRE_APPROVAL
    assert verdict.rule_id == "R012_LOW_CONFIDENCE"


def test_captcha_page_hands_control_to_the_user(
    engine, make_element, make_observation, application_context
):
    """Anti-bot checks are escalated to a human, never solved or evaded."""
    observation = make_observation([make_element()], page_kind=PageKind.CAPTCHA)
    verdict = engine.evaluate(_type(), observation, application_context)
    assert verdict.outcome is PolicyOutcome.REQUIRE_APPROVAL
    assert verdict.rule_id == "R013_INTERVENTION_PAGE"


def test_login_page_hands_control_to_the_user(
    engine, make_element, make_observation, application_context
):
    observation = make_observation([make_element()], page_kind=PageKind.LOGIN)
    verdict = engine.evaluate(_type(), observation, application_context)
    assert verdict.outcome is PolicyOutcome.REQUIRE_APPROVAL


# --------------------------------------------------------------------------------------
# Approval semantics
# --------------------------------------------------------------------------------------


def test_granted_approval_unlocks_that_exact_action(
    engine, make_element, make_observation, application_context
):
    element = make_element(name="Expected salary")
    observation = make_observation([element])
    decision = _type(value="90000")

    assert engine.evaluate(decision, observation, application_context).blocks_execution

    application_context.grant(decision)
    verdict = engine.evaluate(decision, observation, application_context)
    assert verdict.outcome is PolicyOutcome.ALLOW
    assert verdict.rule_id == "R000_HUMAN_APPROVED"


def test_approval_does_not_generalise_to_a_different_value(
    engine, make_element, make_observation, application_context
):
    """Approving "type 90000" must not authorise "type 1"."""
    observation = make_observation([make_element(name="Expected salary")])
    application_context.grant(_type(value="90000"))
    verdict = engine.evaluate(_type(value="1"), observation, application_context)
    assert verdict.outcome is PolicyOutcome.REQUIRE_APPROVAL


def test_approval_does_not_generalise_to_a_different_element(
    engine, make_element, make_observation, application_context
):
    observation = make_observation(
        [make_element(ref="e1", name="Expected salary"),
         make_element(ref="e2", name="Notice period")]
    )
    application_context.grant(_type(ref="e1", value="x"))
    verdict = engine.evaluate(_type(ref="e2", value="x"), observation, application_context)
    assert verdict.outcome is PolicyOutcome.REQUIRE_APPROVAL


def test_approval_cannot_unlock_a_denied_action(
    engine, make_element, make_observation, application_context
):
    """DENY rules run before the approval check, so a human cannot approve their way into
    filling a signature field."""
    observation = make_observation([make_element(name="Electronic signature")])
    decision = _type(value="Haidar Farhat")
    application_context.grant(decision)
    verdict = engine.evaluate(decision, observation, application_context)
    assert verdict.outcome is PolicyOutcome.DENY
    assert verdict.rule_id == "R005_NEVER_AUTOFILL"


def test_approval_cannot_unlock_the_kill_switch(
    engine, make_element, make_observation, application_context
):
    observation = make_observation([make_element()])
    decision = _type()
    application_context.grant(decision)
    KILL_SWITCH.engage("test")
    assert engine.evaluate(decision, observation, application_context).outcome is PolicyOutcome.DENY


# --------------------------------------------------------------------------------------
# ALLOW
# --------------------------------------------------------------------------------------


def test_safe_field_with_high_confidence_is_allowed(
    engine, make_element, make_observation, application_context
):
    observation = make_observation([make_element(name="First name")])
    verdict = engine.evaluate(_type(value="Haidar"), observation, application_context)
    assert verdict.outcome is PolicyOutcome.ALLOW
    assert verdict.rule_id == "R099_DEFAULT_ALLOW"


def test_scrolling_is_always_allowed(engine, make_observation, application_context):
    decision = Decision(action=ActionType.SCROLL, confidence=0.5, reason="look further down")
    verdict = engine.evaluate(decision, make_observation(), application_context)
    assert verdict.outcome is PolicyOutcome.ALLOW
