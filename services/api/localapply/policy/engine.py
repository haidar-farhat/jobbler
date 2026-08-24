"""The policy engine: the boundary between what a model *proposes* and what actually happens.

Contains no LLM call, no network access, and no page content. It is a pure function of
`(Decision, Observation, RunContext)`, which is precisely why nothing written on a web page
can weaken it. See docs/adr/0001-observe-reason-policy-execute.md.

Evaluation order matters and is deliberate:

    1. DENY rules        -- absolute. Not unlockable by a human.
    2. Human approval    -- a granted approval for this exact decision.
    3. APPROVAL rules    -- gates a human may unlock.
    4. Default           -- allow.

Denials are checked *before* approvals so that approving one action can never smuggle through
a categorically forbidden one (a signature field stays forbidden even mid-approved-run).
"""

from __future__ import annotations

from ..contracts import Decision, Observation, PolicyOutcome, PolicyVerdict
from .rules import APPROVAL_RULES, DENY_RULES, RunContext


class PolicyEngine:
    def __init__(self, deny_rules=None, approval_rules=None) -> None:
        self._deny_rules = list(DENY_RULES if deny_rules is None else deny_rules)
        self._approval_rules = list(APPROVAL_RULES if approval_rules is None else approval_rules)

    def evaluate(
        self, decision: Decision, observation: Observation, context: RunContext
    ) -> PolicyVerdict:
        for rule in self._deny_rules:
            verdict = rule(decision, observation, context)
            if verdict is not None:
                return verdict

        # Only reached once every DENY rule has passed.
        if context.is_granted(decision):
            return PolicyVerdict(
                outcome=PolicyOutcome.ALLOW,
                rule_id="R000_HUMAN_APPROVED",
                reason="Explicitly approved by the user.",
            )

        for rule in self._approval_rules:
            verdict = rule(decision, observation, context)
            if verdict is not None:
                return verdict

        return PolicyVerdict(
            outcome=PolicyOutcome.ALLOW,
            rule_id="R099_DEFAULT_ALLOW",
            reason="No rule objected.",
        )
