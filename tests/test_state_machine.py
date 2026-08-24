"""The application state machine."""

from __future__ import annotations

import pytest
from localapply.orchestrator.state_machine import (
    TERMINAL_STATES,
    TRANSITIONS,
    ApplicationState,
    InvalidTransition,
    can_transition,
    transition,
)

S = ApplicationState


def test_happy_path_runs_end_to_end():
    path = [
        S.DISCOVERED, S.PARSED, S.ANALYZED, S.SCORED, S.RECOMMENDED, S.USER_APPROVED,
        S.DOCUMENTS_GENERATING, S.READY_FOR_BROWSER, S.BROWSER_RUNNING, S.FORM_ANALYZED,
        S.SAFE_FIELDS_FILLED, S.REVIEW_REQUIRED, S.SUBMITTING, S.SUBMITTED,
    ]
    state = path[0]
    for target in path[1:]:
        state = transition(state, target)
    assert state is S.SUBMITTED


def test_submitting_is_only_reachable_through_review_required():
    """The structural guarantee behind human-in-the-loop: there is no state from which the
    application can start submitting without having stopped for review first."""
    sources = [s for s, targets in TRANSITIONS.items() if S.SUBMITTING in targets]
    assert sources == [S.REVIEW_REQUIRED]


def test_submitted_is_only_reachable_from_submitting():
    sources = [s for s, targets in TRANSITIONS.items() if S.SUBMITTED in targets]
    assert sources == [S.SUBMITTING]


def test_cannot_skip_the_review_gate():
    with pytest.raises(InvalidTransition):
        transition(S.SAFE_FIELDS_FILLED, S.SUBMITTING)


def test_cannot_jump_straight_to_submitted():
    with pytest.raises(InvalidTransition):
        transition(S.DISCOVERED, S.SUBMITTED)


def test_terminal_states_go_nowhere():
    for state in TERMINAL_STATES:
        assert TRANSITIONS[state] == frozenset()


def test_any_live_state_can_be_interrupted():
    for state in S:
        if state in TERMINAL_STATES:
            continue
        assert can_transition(state, S.BLOCKED)
        assert can_transition(state, S.CANCELLED)


def test_blocked_work_resumes_through_user_intervention():
    state = transition(S.FORM_ANALYZED, S.BLOCKED)
    state = transition(state, S.USER_INTERVENTION)
    assert can_transition(state, S.FORM_ANALYZED)
    # ...but never straight back into a completed submission.
    assert not can_transition(state, S.SUBMITTED)


def test_rejected_review_goes_backwards_not_forwards():
    assert can_transition(S.REVIEW_REQUIRED, S.SAFE_FIELDS_FILLED)


def test_a_failed_submit_does_not_become_submitted():
    assert can_transition(S.SUBMITTING, S.FAILED)
    assert can_transition(S.SUBMITTING, S.BLOCKED)


def test_invalid_transition_message_lists_what_is_allowed():
    with pytest.raises(InvalidTransition, match="Allowed from discovered"):
        transition(S.DISCOVERED, S.SUBMITTED)
