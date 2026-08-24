"""Database tables for the walking skeleton.

A subset of the full design (§37): only what the Observe -> Reason -> Policy -> Execute loop
actually needs. Documents, generated CVs, model runs, and notifications land with their own
phases rather than sitting empty now.

Enums are stored as plain strings rather than native Postgres enums so the same schema runs
on SQLite, which lets the whole test suite execute without Docker.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlmodel import Column, Field, SQLModel


def utc_now() -> datetime:
    return datetime.now(UTC)


def _ts_column(**kw) -> Column:
    return Column(sa.DateTime(timezone=True), nullable=False, **kw)


# --------------------------------------------------------------------------------------
# Profile — the professional knowledge base
# --------------------------------------------------------------------------------------


class Profile(SQLModel, table=True):
    __tablename__ = "profiles"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    full_name: str
    email: str
    created_at: datetime = Field(default_factory=utc_now, sa_column=_ts_column())
    updated_at: datetime = Field(default_factory=utc_now, sa_column=_ts_column())


class Document(SQLModel, table=True):
    """An uploaded CV or supporting document.

    Extracted text is stored alongside the original so a re-parse never needs another
    upload, and so any proposed fact can be audited against its source.
    """

    __tablename__ = "documents"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    profile_id: UUID = Field(foreign_key="profiles.id", index=True)
    filename: str
    content_type: str = ""
    size_bytes: int = 0
    #: Content hash, so re-uploading the same file is recognised rather than duplicated.
    sha256: str = Field(index=True)
    stored_path: str = ""
    parser: str = ""  # pdf | docx | text
    text: str = ""
    text_chars: int = 0
    page_count: int | None = None
    error: str | None = None
    uploaded_at: datetime = Field(default_factory=utc_now, sa_column=_ts_column())


class ProfileFact(SQLModel, table=True):
    """One atomic, individually-provenanced fact.

    The profile is a set of facts rather than a blob of text, so a CV import can *propose*
    additions you approve one at a time instead of silently rewriting your identity.

    `status` is the only gate on use -- see profile.facts.is_usable(). There is deliberately
    no second boolean beside it that could fall out of step.
    """

    __tablename__ = "profile_facts"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    profile_id: UUID = Field(foreign_key="profiles.id", index=True)
    #: Matches field_classifier.SAFE_PATTERNS profile keys, e.g. "first_name".
    key: str = Field(index=True)
    value: str
    category: str = "identity"  # see profile.facts.FactCategory
    source: str = "manual"  # see profile.facts.FactSource
    confidence: float = 1.0
    #: accepted | proposed | rejected | superseded. Only "accepted" is usable.
    status: str = Field(default="proposed", index=True)
    #: Provenance: which upload produced this fact, when it came from one.
    document_id: UUID | None = Field(default=None, foreign_key="documents.id", index=True)
    #: Set on a proposal that would replace an existing accepted fact with the same key.
    supersedes_id: UUID | None = Field(default=None, foreign_key="profile_facts.id")
    #: The line it was found on, so a proposal can be checked against the source text.
    evidence: str | None = None
    created_at: datetime = Field(default_factory=utc_now, sa_column=_ts_column())
    resolved_at: datetime | None = Field(
        default=None, sa_column=Column(sa.DateTime(timezone=True), nullable=True)
    )


# --------------------------------------------------------------------------------------
# Jobs and applications
# --------------------------------------------------------------------------------------


class Job(SQLModel, table=True):
    __tablename__ = "jobs"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    source: str = "fixture"
    external_id: str | None = Field(default=None, index=True)
    url: str
    title: str
    company: str | None = None
    location: str | None = None
    description: str | None = None
    requirements: list = Field(default_factory=list, sa_column=Column(sa.JSON))
    match_score: float | None = None
    match_breakdown: dict = Field(default_factory=dict, sa_column=Column(sa.JSON))
    discovered_at: datetime = Field(default_factory=utc_now, sa_column=_ts_column())


class Application(SQLModel, table=True):
    __tablename__ = "applications"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    job_id: UUID = Field(foreign_key="jobs.id", index=True)
    profile_id: UUID = Field(foreign_key="profiles.id", index=True)
    #: Written only via orchestrator.state_machine.transition().
    state: str = Field(default="discovered", index=True)
    #: Where to resume after a BLOCKED / USER_INTERVENTION detour.
    resume_state: str | None = None
    created_at: datetime = Field(default_factory=utc_now, sa_column=_ts_column())
    updated_at: datetime = Field(default_factory=utc_now, sa_column=_ts_column())
    submitted_at: datetime | None = Field(
        default=None, sa_column=Column(sa.DateTime(timezone=True), nullable=True)
    )
    #: True when DRY_RUN suppressed the real submit click.
    simulated: bool = True


# --------------------------------------------------------------------------------------
# Agent runs
# --------------------------------------------------------------------------------------


class AgentRun(SQLModel, table=True):
    __tablename__ = "agent_runs"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    application_id: UUID | None = Field(default=None, foreign_key="applications.id", index=True)
    agent: str = "application"
    goal: str = ""
    status: str = Field(default="running", index=True)  # running|paused|waiting|finished|failed
    start_url: str = ""
    actions_executed: int = 0
    error: str | None = None
    started_at: datetime = Field(default_factory=utc_now, sa_column=_ts_column())
    finished_at: datetime | None = Field(
        default=None, sa_column=Column(sa.DateTime(timezone=True), nullable=True)
    )


class AgentEventRow(SQLModel, table=True):
    """Append-only. Never updated, never deleted."""

    __tablename__ = "agent_events"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    run_id: UUID = Field(foreign_key="agent_runs.id", index=True)
    seq: int = Field(index=True)
    type: str = Field(index=True)
    agent: str = "orchestrator"
    message: str = ""
    payload: dict = Field(default_factory=dict, sa_column=Column(sa.JSON))
    ts: datetime = Field(default_factory=utc_now, sa_column=_ts_column())


class BrowserSessionRow(SQLModel, table=True):
    __tablename__ = "browser_sessions"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    run_id: UUID = Field(foreign_key="agent_runs.id", index=True)
    start_url: str = ""
    closed: bool = False
    created_at: datetime = Field(default_factory=utc_now, sa_column=_ts_column())


class BrowserAction(SQLModel, table=True):
    """One executed action. The row exists only if the executor actually ran it, so an empty
    result set is proof that nothing happened."""

    __tablename__ = "browser_actions"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    run_id: UUID = Field(foreign_key="agent_runs.id", index=True)
    session_id: UUID | None = Field(default=None, foreign_key="browser_sessions.id")
    action: str = Field(index=True)
    target_ref: str | None = None
    target_name: str | None = None
    #: Never stores values from NEVER_AUTOFILL fields; see run_loop._redact.
    value: str | None = None
    success: bool = True
    simulated: bool = False
    error: str | None = None
    duration_ms: int = 0
    policy_rule: str | None = None
    ts: datetime = Field(default_factory=utc_now, sa_column=_ts_column())


class Screenshot(SQLModel, table=True):
    __tablename__ = "screenshots"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    run_id: UUID = Field(foreign_key="agent_runs.id", index=True)
    path: str
    url: str = ""
    ts: datetime = Field(default_factory=utc_now, sa_column=_ts_column())


class Approval(SQLModel, table=True):
    """A human decision on one specific proposed action.

    `fingerprint` ties the approval to exactly that action, so approving "type 90000 into the
    salary field" cannot be replayed to authorise anything else.
    """

    __tablename__ = "approvals"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    run_id: UUID = Field(foreign_key="agent_runs.id", index=True)
    fingerprint: str = Field(index=True)
    action: str
    target_ref: str | None = None
    target_name: str | None = None
    proposed_value: str | None = None
    reason: str = ""
    policy_rule: str = ""
    field_class: str | None = None
    status: str = Field(default="pending", index=True)  # pending | approved | rejected
    #: Set when the user edits the value before approving.
    edited_value: str | None = None
    resolved_by: str | None = None
    created_at: datetime = Field(default_factory=utc_now, sa_column=_ts_column())
    resolved_at: datetime | None = Field(
        default=None, sa_column=Column(sa.DateTime(timezone=True), nullable=True)
    )


class AuditLog(SQLModel, table=True):
    """Who did what, from where. Every remote action lands here (§36)."""

    __tablename__ = "audit_logs"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    actor: str = "user"
    source: str = "web"  # web | phone | system
    action: str
    target: str | None = None
    result: str = "ok"
    detail: dict = Field(default_factory=dict, sa_column=Column(sa.JSON))
    ts: datetime = Field(default_factory=utc_now, sa_column=_ts_column())


__all__ = [
    "AgentEventRow",
    "AgentRun",
    "Application",
    "Approval",
    "AuditLog",
    "BrowserAction",
    "BrowserSessionRow",
    "Job",
    "Profile",
    "ProfileFact",
    "Screenshot",
]
