"""The four core contracts of the Observe -> Reason -> Policy -> Execute loop.

This module is the **single source of truth** for the agent protocol. JSON Schema for
`packages/shared-types` and the TypeScript types consumed by the web app are both generated
from these models (see `scripts/export_schemas.py`) rather than hand-maintained in parallel.

Nothing in this module imports the browser, the database, or a model provider. It is pure
data, so every layer can depend on it without depending on each other.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


def _utcnow() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------------------
# Observation — what the page currently is
# --------------------------------------------------------------------------------------


class ElementRole(str, Enum):
    """Coarse interaction role. Deliberately small: the reasoner decides *what to do*,
    not *how the widget is implemented*."""

    BUTTON = "button"
    LINK = "link"
    TEXTBOX = "textbox"
    TEXTAREA = "textarea"
    CHECKBOX = "checkbox"
    RADIO = "radio"
    COMBOBOX = "combobox"
    FILE_INPUT = "file_input"
    HEADING = "heading"
    OTHER = "other"


class PageKind(str, Enum):
    JOB_LISTING = "job_listing"
    JOB_DETAIL = "job_detail"
    APPLICATION_FORM = "application_form"
    LOGIN = "login"
    CAPTCHA = "captcha"
    CONFIRMATION = "confirmation"
    ERROR = "error"
    UNKNOWN = "unknown"


class ObservedElement(BaseModel):
    """One interactive element, addressed only by its opaque ref.

    `ref` is an observer-assigned handle (`e1`, `e2`, ...) valid **only for the observation
    that produced it**. It is not a selector and carries no information about the page's
    structure. See docs/adr/0002-opaque-element-refs.md.
    """

    model_config = ConfigDict(frozen=True)

    ref: str = Field(pattern=r"^e\d+$", description="Opaque element handle, e.g. 'e17'.")
    role: ElementRole
    name: str = Field(description="Accessible name: aria-label, <label>, placeholder, or text.")
    value: str | None = None
    input_type: str | None = Field(
        default=None, description="Raw HTML input type, e.g. 'email'. Feeds field classification."
    )
    enabled: bool = True
    required: bool = False
    visible: bool = True
    options: list[str] = Field(
        default_factory=list, description="Choices, for combobox/radio groups."
    )


class Observation(BaseModel):
    """The observer's complete report on the current page. Contains no judgement."""

    observation_id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    url: str
    title: str
    page_kind: PageKind = PageKind.UNKNOWN
    screenshot_id: UUID | None = None
    elements: list[ObservedElement] = Field(default_factory=list)
    untrusted_text: str = Field(
        default="",
        description=(
            "Visible page text. UNTRUSTED third-party input: it is wrapped in "
            "<UNTRUSTED_WEB_CONTENT> tags before ever reaching a model, and is treated as "
            "data, never as instruction."
        ),
    )
    ts: datetime = Field(default_factory=_utcnow)

    def element(self, ref: str) -> ObservedElement | None:
        return next((e for e in self.elements if e.ref == ref), None)

    def refs(self) -> set[str]:
        return {e.ref for e in self.elements}


# --------------------------------------------------------------------------------------
# Decision — what the reasoner proposes
# --------------------------------------------------------------------------------------


class ActionType(str, Enum):
    NAVIGATE = "navigate"
    CLICK = "click"
    TYPE = "type"
    SCROLL = "scroll"
    SELECT = "select"
    UPLOAD = "upload"
    WAIT = "wait"
    EXTRACT = "extract"
    SCREENSHOT = "screenshot"
    # SUBMIT is a distinct action precisely so it can be gated in exactly one place.
    SUBMIT = "submit"
    # Terminal / control actions.
    ASK_USER = "ask_user"
    FINISH = "finish"


#: Actions that mutate the page or the outside world. Used by the policy engine.
MUTATING_ACTIONS: frozenset[ActionType] = frozenset(
    {
        ActionType.CLICK,
        ActionType.TYPE,
        ActionType.SELECT,
        ActionType.UPLOAD,
        ActionType.SUBMIT,
    }
)

#: Actions that must name a target element. SUBMIT is included so that the submit button is
#: itself ref-validated -- there is no "submit the form somehow" action.
TARGETED_ACTIONS: frozenset[ActionType] = frozenset(
    {
        ActionType.CLICK,
        ActionType.TYPE,
        ActionType.SELECT,
        ActionType.UPLOAD,
        ActionType.SUBMIT,
    }
)


class Decision(BaseModel):
    """A *proposal*. It has no effect until the policy engine allows it.

    The reasoner produces this from an `Observation` alone. It may name only a `target_ref`
    drawn from that observation -- never a selector, never a coordinate.
    """

    model_config = ConfigDict(frozen=True)

    action: ActionType
    target_ref: str | None = Field(default=None, pattern=r"^e\d+$")
    value: str | None = Field(default=None, description="Text to type, option to select, etc.")
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(min_length=1, description="Why. Surfaced verbatim in the UI and log.")


# --------------------------------------------------------------------------------------
# PolicyVerdict — what the (non-model) policy engine permits
# --------------------------------------------------------------------------------------


class PolicyOutcome(str, Enum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


class PolicyVerdict(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: PolicyOutcome
    rule_id: str = Field(description="Which rule decided, e.g. 'R004_SUBMIT_ALWAYS_GATED'.")
    reason: str
    #: Field-safety classification, when the decision targeted a form field.
    field_class: str | None = None

    @property
    def blocks_execution(self) -> bool:
        return self.outcome is not PolicyOutcome.ALLOW


# --------------------------------------------------------------------------------------
# ActionResult — what actually happened
# --------------------------------------------------------------------------------------


class ActionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    action: ActionType
    success: bool
    new_page_state: str | None = Field(default=None, description="URL after the action.")
    error: str | None = None
    duration_ms: int = 0
    #: True when DRY_RUN suppressed a real side effect (SUBMIT is simulated, never clicked).
    simulated: bool = False


# --------------------------------------------------------------------------------------
# Agent events — the append-only stream that drives the UI and enables replay
# --------------------------------------------------------------------------------------


class EventType(str, Enum):
    RUN_STARTED = "run_started"
    OBSERVATION = "observation"
    DECISION = "decision"
    POLICY_VERDICT = "policy_verdict"
    ACTION_RESULT = "action_result"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_RESOLVED = "approval_resolved"
    STATE_CHANGED = "state_changed"
    RUN_PAUSED = "run_paused"
    RUN_RESUMED = "run_resumed"
    RUN_FINISHED = "run_finished"
    RUN_FAILED = "run_failed"
    KILL_SWITCH = "kill_switch"
    LOG = "log"


class AgentEvent(BaseModel):
    """One entry in the append-only run log. Same shape on the SSE wire and in the DB."""

    event_id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    seq: int = Field(default=0, description="Monotonic per run; the UI orders by this.")
    type: EventType
    agent: str = Field(default="orchestrator")
    message: str = ""
    payload: dict = Field(default_factory=dict)
    ts: datetime = Field(default_factory=_utcnow)


__all__ = [
    "MUTATING_ACTIONS",
    "TARGETED_ACTIONS",
    "ActionResult",
    "ActionType",
    "AgentEvent",
    "Decision",
    "ElementRole",
    "EventType",
    "ObservedElement",
    "Observation",
    "PageKind",
    "PolicyOutcome",
    "PolicyVerdict",
]
