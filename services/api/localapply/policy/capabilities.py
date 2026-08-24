"""Per-agent capability sets.

Every agent gets an explicit allowlist of actions. This is the "no agent has unrestricted
computer access" property: the application agent literally cannot navigate away to an
arbitrary URL, and the discovery agent literally cannot type into a form.
"""

from __future__ import annotations

from ..contracts import ActionType

A = ActionType

#: Actions any agent may take -- pure observation and control flow.
_READ_ONLY: frozenset[ActionType] = frozenset(
    {A.SCROLL, A.WAIT, A.EXTRACT, A.SCREENSHOT, A.ASK_USER, A.FINISH}
)

CAPABILITIES: dict[str, frozenset[ActionType]] = {
    # Finds and reads job listings. May open jobs; may never touch a form.
    "discovery": _READ_ONLY | {A.NAVIGATE, A.CLICK},
    # Scores jobs against the profile. Reads only -- it does not drive the browser at all.
    "analysis": frozenset({A.EXTRACT, A.FINISH}),
    # Fills applications. May type, select, and upload -- and may *propose* SUBMIT, which the
    # policy engine then always routes through human approval (R010).
    "application": _READ_ONLY | {A.NAVIGATE, A.CLICK, A.TYPE, A.SELECT, A.UPLOAD, A.SUBMIT},
    # Coordinates; never drives the browser itself.
    "orchestrator": frozenset({A.ASK_USER, A.FINISH}),
}


def capabilities_for(agent: str) -> frozenset[ActionType]:
    """Unknown agents get nothing. Fail closed."""
    return CAPABILITIES.get(agent, frozenset())
