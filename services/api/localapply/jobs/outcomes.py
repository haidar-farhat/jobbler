"""What happened after you applied, and what it tells you.

The application state machine ends at SUBMITTED. Everything interesting to a person happens
after that, and until now none of it was recorded -- so the app could apply to two hundred
jobs and never tell you whether any of it worked.

Two rules keep this from becoming a mess:

  * **An outcome is not a state.** `applications.state` still has exactly two writers. An
    outcome lives in its own table, is recorded by a person days later, and cannot be
    validated by any machine -- so it is not something the state machine should pretend to
    own.
  * **Nothing here is inferred except silence.** "They have not replied in five weeks" is a
    fact about the calendar and is derived. Everything else is typed in by the person who
    read the email.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ..db import models as m


class OutcomeKind(str, Enum):
    """The shapes a real process takes. Ordered from least to most advanced, which is what
    lets "furthest reached" be a comparison rather than a lookup table."""

    APPLIED = "applied"
    REPLIED = "replied"
    SCREENING = "screening"
    INTERVIEWED = "interviewed"
    OFFER = "offer"
    ACCEPTED = "accepted"
    #: Endings. Not ranked against the above -- a rejection after an interview is further
    #: along than a rejection after a screen, and `furthest` is what says so.
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    #: Derived, never typed. See `GHOSTED_AFTER`.
    GHOSTED = "ghosted"


#: Progress, for "how far did this get". The endings are absent on purpose: they say how a
#: process stopped, not how far it went, and ranking them would make a rejection after an
#: offer look like a step backwards from a screen.
PROGRESS: dict[str, int] = {
    OutcomeKind.APPLIED.value: 0,
    OutcomeKind.REPLIED.value: 1,
    OutcomeKind.SCREENING.value: 2,
    OutcomeKind.INTERVIEWED.value: 3,
    OutcomeKind.OFFER.value: 4,
    OutcomeKind.ACCEPTED.value: 5,
}

CLOSED = frozenset({OutcomeKind.REJECTED.value, OutcomeKind.WITHDRAWN.value,
                    OutcomeKind.ACCEPTED.value, OutcomeKind.GHOSTED.value})

#: Silence this long is an answer. Five weeks is deliberately generous: the cost of calling
#: a live application dead is worse than the cost of leaving a dead one open a fortnight
#: longer, and nothing is written to the database when this fires -- it is a view, so a reply
#: on week six simply un-ghosts it.
GHOSTED_AFTER = timedelta(weeks=5)

#: Score bands for "does a better match actually get replies". Four buckets, because with
#: forty applications ten buckets each hold four and mean nothing.
BANDS = ((0.0, 0.4, "under 40%"), (0.4, 0.6, "40-60%"), (0.6, 0.8, "60-80%"), (0.8, 1.01, "80%+"))


def aware(value: datetime | None) -> datetime | None:
    """Guarantee a comparable timestamp, whatever the database gave back.

    Every timestamp column here is `DateTime(timezone=True)`, and Postgres honours that.
    SQLite has no timezone type and hands back a naive datetime from the same column, so the
    same arithmetic that works in production raises `can't subtract offset-naive and
    offset-aware datetimes` against the test database -- and would raise the same way for
    anyone running the app on SQLite.

    Naive values are read as UTC because that is what was written: `utc_now()` is the only
    writer.
    """
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


@dataclass
class Story:
    """One application's history, read as a whole rather than as rows."""

    application_id: UUID
    events: list[m.ApplicationOutcome]
    submitted_at: datetime | None

    def __post_init__(self) -> None:
        self.submitted_at = aware(self.submitted_at)

    @property
    def current(self) -> str:
        if not self.events:
            return OutcomeKind.APPLIED.value
        return self.events[-1].kind

    @property
    def furthest(self) -> str:
        """How far this got, ignoring how it ended."""
        ranked = [e.kind for e in self.events if e.kind in PROGRESS]
        if not ranked:
            return OutcomeKind.APPLIED.value
        return max(ranked, key=lambda k: PROGRESS[k])

    @property
    def heard_back(self) -> bool:
        return any(PROGRESS.get(e.kind, 0) >= 1 for e in self.events)

    @property
    def closed(self) -> bool:
        return self.current in CLOSED

    @property
    def days_to_reply(self) -> float | None:
        """How long they took, in days. `None` when they have not."""
        if not self.submitted_at:
            return None
        first = next((e for e in self.events if PROGRESS.get(e.kind, 0) >= 1), None)
        if first is None:
            return None
        happened = aware(first.occurred_at)
        return round((happened - self.submitted_at).total_seconds() / 86400, 1)

    @property
    def status(self) -> str:
        """What to show. The only derived value, and it is derived from the clock."""
        if self.closed:
            return self.current
        if self.heard_back:
            return self.furthest
        if self.submitted_at and datetime.now(UTC) - self.submitted_at > GHOSTED_AFTER:
            return OutcomeKind.GHOSTED.value
        return OutcomeKind.APPLIED.value

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "furthest": self.furthest,
            "heard_back": self.heard_back,
            "closed": self.closed,
            "days_to_reply": self.days_to_reply,
            "events": [
                {
                    "kind": e.kind,
                    "note": e.note,
                    "occurred_at": aware(e.occurred_at).isoformat(),
                }
                for e in self.events
            ],
        }


async def record(
    session: AsyncSession,
    application: m.Application,
    kind: str,
    *,
    note: str = "",
    occurred_at: datetime | None = None,
) -> m.ApplicationOutcome:
    """Add one event to an application's history.

    `applications.state` is untouched. That is the point.
    """
    if kind not in {k.value for k in OutcomeKind}:
        raise ValueError(f"Unknown outcome {kind!r}.")
    if kind == OutcomeKind.GHOSTED.value:
        raise ValueError(
            "Ghosting is not something you record -- it is what silence past five weeks "
            "already means, and a reply after that simply undoes it."
        )

    event = m.ApplicationOutcome(
        application_id=application.id,
        kind=kind,
        note=" ".join((note or "").split())[:500],
        occurred_at=occurred_at or datetime.now(UTC),
    )
    session.add(event)
    await session.commit()
    return event


async def story_for(session: AsyncSession, application: m.Application) -> Story:
    rows = await session.execute(
        select(m.ApplicationOutcome)
        .where(m.ApplicationOutcome.application_id == application.id)
        .order_by(m.ApplicationOutcome.occurred_at)
    )
    return Story(
        application_id=application.id,
        events=list(rows.scalars().all()),
        submitted_at=application.submitted_at,
    )


async def stories_for(
    session: AsyncSession, applications: list[m.Application]
) -> dict[UUID, Story]:
    """Every application's history in one query.

    Per-application queries here would be one round trip per row on a board of five hundred,
    which is the shape of every list page that gets slow six months in.
    """
    if not applications:
        return {}
    ids = [a.id for a in applications]
    rows = await session.execute(
        select(m.ApplicationOutcome)
        .where(m.ApplicationOutcome.application_id.in_(ids))
        .order_by(m.ApplicationOutcome.occurred_at)
    )
    grouped: dict[UUID, list[m.ApplicationOutcome]] = defaultdict(list)
    for event in rows.scalars().all():
        grouped[event.application_id].append(event)

    return {
        a.id: Story(application_id=a.id, events=grouped.get(a.id, []),
                    submitted_at=a.submitted_at)
        for a in applications
    }


# --------------------------------------------------------------------------------------
# What it all adds up to
# --------------------------------------------------------------------------------------


def band_for(score: float | None) -> str:
    if score is None:
        return "unscored"
    for low, high, label in BANDS:
        if low <= score < high:
            return label
    return "80%+"


@dataclass
class Tally:
    sent: int = 0
    heard_back: int = 0
    interviewed: int = 0
    offers: int = 0
    rejected: int = 0
    ghosted: int = 0
    reply_days: list[float] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.reply_days is None:
            self.reply_days = []

    def add(self, story: Story) -> None:
        self.sent += 1
        if story.heard_back:
            self.heard_back += 1
        if PROGRESS.get(story.furthest, 0) >= PROGRESS[OutcomeKind.INTERVIEWED.value]:
            self.interviewed += 1
        if PROGRESS.get(story.furthest, 0) >= PROGRESS[OutcomeKind.OFFER.value]:
            self.offers += 1
        if story.current == OutcomeKind.REJECTED.value:
            self.rejected += 1
        if story.status == OutcomeKind.GHOSTED.value:
            self.ghosted += 1
        days = story.days_to_reply
        if days is not None:
            self.reply_days.append(days)

    def as_dict(self) -> dict:
        return {
            "sent": self.sent,
            "heard_back": self.heard_back,
            "interviewed": self.interviewed,
            "offers": self.offers,
            "rejected": self.rejected,
            "ghosted": self.ghosted,
            # A rate is a fraction of what was sent, and a fraction of nothing is not zero.
            "reply_rate": round(self.heard_back / self.sent, 3) if self.sent else None,
            "median_reply_days": _median(self.reply_days),
        }


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return round((ordered[middle - 1] + ordered[middle]) / 2, 1)


#: Below this, a rate is noise. Three applications with one reply is not a 33% reply rate,
#: and showing it as one invites a decision the data cannot support.
MIN_FOR_A_RATE = 5


def summarise(rows: list[tuple[m.Job, m.Application, Story]]) -> dict:
    """Answer the one question the app exists to answer: is this working?"""
    overall = Tally()
    by_band: dict[str, Tally] = defaultdict(Tally)
    by_company: dict[str, Tally] = defaultdict(Tally)
    by_source: dict[str, Tally] = defaultdict(Tally)

    for job, _application, story in rows:
        overall.add(story)
        by_band[band_for(job.match_score)].add(story)
        by_company[job.company or "unknown"].add(story)
        by_source[job.source].add(story)

    def shown(grouped: dict[str, Tally], minimum: int = 1) -> list[dict]:
        return [
            {"group": name, **tally.as_dict(),
             "enough_to_judge": tally.sent >= MIN_FOR_A_RATE}
            for name, tally in sorted(grouped.items(), key=lambda kv: -kv[1].sent)
            if tally.sent >= minimum
        ]

    ordered_bands = [label for _, _, label in BANDS] + ["unscored"]
    return {
        "overall": overall.as_dict(),
        "enough_to_judge": overall.sent >= MIN_FOR_A_RATE,
        "min_for_a_rate": MIN_FOR_A_RATE,
        # The headline comparison: does a better match actually get replies?
        "by_match_score": sorted(
            shown(by_band),
            key=lambda row: ordered_bands.index(row["group"])
            if row["group"] in ordered_bands
            else 99,
        ),
        "by_company": shown(by_company, minimum=2)[:15],
        "by_source": shown(by_source),
    }
