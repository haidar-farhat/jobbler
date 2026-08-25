"""The application state machine.

Transitions are validated **here and nowhere else**. Agent code never writes
`applications.state` directly -- it calls `transition()`, which raises on an illegal move.
That is what makes the state column trustworthy enough to drive the UI and the audit log.

Two wrappers call it, and only two: `jobs.pipeline.advance` for the pre-browser half, and
`RunManager._transition` once a browser is involved. Creating a row seeds the initial state
from the column default and is not a transition -- there is nothing to transition from.
"""

from __future__ import annotations

from enum import Enum


class ApplicationState(str, Enum):
    DISCOVERED = "discovered"
    PARSED = "parsed"
    ANALYZED = "analyzed"
    SCORED = "scored"
    RECOMMENDED = "recommended"
    USER_APPROVED = "user_approved"
    DOCUMENTS_GENERATING = "documents_generating"
    READY_FOR_BROWSER = "ready_for_browser"
    BROWSER_RUNNING = "browser_running"
    FORM_ANALYZED = "form_analyzed"
    SAFE_FIELDS_FILLED = "safe_fields_filled"
    REVIEW_REQUIRED = "review_required"
    SUBMITTING = "submitting"
    SUBMITTED = "submitted"

    # Off-happy-path
    BLOCKED = "blocked"
    USER_INTERVENTION = "user_intervention"
    FAILED = "failed"
    CANCELLED = "cancelled"


S = ApplicationState

#: The happy path, in order. `REVIEW_REQUIRED -> SUBMITTING` is the human-approval gate:
#: there is no transition into SUBMITTING that does not pass through it.
HAPPY_PATH: list[ApplicationState] = [
    S.DISCOVERED,
    S.PARSED,
    S.ANALYZED,
    S.SCORED,
    S.RECOMMENDED,
    S.USER_APPROVED,
    S.DOCUMENTS_GENERATING,
    S.READY_FOR_BROWSER,
    S.BROWSER_RUNNING,
    S.FORM_ANALYZED,
    S.SAFE_FIELDS_FILLED,
    S.REVIEW_REQUIRED,
    S.SUBMITTING,
    S.SUBMITTED,
]

#: States from which nothing further may happen.
TERMINAL_STATES: frozenset[ApplicationState] = frozenset({S.SUBMITTED, S.FAILED, S.CANCELLED})

#: Any non-terminal state may be interrupted. Recovery goes through USER_INTERVENTION.
_INTERRUPTS: frozenset[ApplicationState] = frozenset({S.BLOCKED, S.FAILED, S.CANCELLED})


def _build_transitions() -> dict[ApplicationState, frozenset[ApplicationState]]:
    table: dict[ApplicationState, set[ApplicationState]] = {s: set() for s in S}

    for current, nxt in zip(HAPPY_PATH, HAPPY_PATH[1:], strict=False):
        table[current].add(nxt)

    # Any live state can be interrupted.
    for state in S:
        if state not in TERMINAL_STATES:
            table[state] |= set(_INTERRUPTS)

    # Blocked work waits for a human, then resumes wherever it left off -- but resuming
    # must not become a side door into submission. A run that detoured through a CAPTCHA or
    # a login wall re-enters the happy path *before* the review gate and passes through it
    # again, so REVIEW_REQUIRED remains the only predecessor of SUBMITTING.
    table[S.BLOCKED].add(S.USER_INTERVENTION)
    # REVIEW_REQUIRED is excluded alongside SUBMITTING and SUBMITTED, or the comment above
    # is not true: resuming straight onto the gate state would let a run that detoured
    # through a CAPTCHA arrive at the point where the only thing left is to submit, without
    # having re-filled and re-checked anything. The run loop never uses that edge -- it
    # resumes to FORM_ANALYZED -- so removing it costs nothing and closes the side door.
    table[S.USER_INTERVENTION] |= {
        s for s in HAPPY_PATH
        if s not in {S.REVIEW_REQUIRED, S.SUBMITTING, S.SUBMITTED}
    }
    table[S.USER_INTERVENTION] |= {S.FAILED, S.CANCELLED}

    # A rejected review sends the application back for another pass, not forward.
    table[S.REVIEW_REQUIRED] |= {S.SAFE_FIELDS_FILLED, S.USER_INTERVENTION}

    # A submit attempt that fails is recoverable; it must not silently become SUBMITTED.
    table[S.SUBMITTING] |= {S.BLOCKED, S.FAILED}

    return {state: frozenset(nexts) for state, nexts in table.items()}


TRANSITIONS: dict[ApplicationState, frozenset[ApplicationState]] = _build_transitions()


class InvalidTransition(ValueError):
    def __init__(self, current: ApplicationState, target: ApplicationState) -> None:
        allowed = ", ".join(sorted(s.value for s in TRANSITIONS[current])) or "(none)"
        super().__init__(
            f"Illegal application state transition {current.value} -> {target.value}. "
            f"Allowed from {current.value}: {allowed}"
        )
        self.current = current
        self.target = target


def can_transition(current: ApplicationState, target: ApplicationState) -> bool:
    return target in TRANSITIONS[current]


def transition(current: ApplicationState, target: ApplicationState) -> ApplicationState:
    """Return `target` if the move is legal, else raise `InvalidTransition`."""
    if not can_transition(current, target):
        raise InvalidTransition(current, target)
    return target
