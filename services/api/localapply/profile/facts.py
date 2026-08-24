"""The professional knowledge base: what a fact is, and how one becomes usable.

The profile is a set of individually-provenanced facts, not a blob of text. That is what
makes CV import safe: an importer *proposes*, and nothing it proposes can reach an
application until you have accepted it one fact at a time.

`status` is the single source of truth for whether the agent may use a fact. There is no
second boolean to drift out of step with it -- `is_usable()` is the only test, and
`USABLE_STATUSES` the only set that answers yes.
"""

from __future__ import annotations

from enum import Enum


class FactStatus(str, Enum):
    #: You approved it. The agent may enter this into an application.
    ACCEPTED = "accepted"
    #: Extracted from a document and awaiting your review. Never used by the agent.
    PROPOSED = "proposed"
    #: You said no. Kept so the same extraction is not proposed at you again.
    REJECTED = "rejected"
    #: Replaced by a newer accepted fact. Kept for history.
    SUPERSEDED = "superseded"


#: The whole gate. A fact is usable if and only if its status is in here.
USABLE_STATUSES: frozenset[str] = frozenset({FactStatus.ACCEPTED.value})


def is_usable(status: str) -> bool:
    return status in USABLE_STATUSES


class FactCategory(str, Enum):
    IDENTITY = "identity"
    SKILL = "skill"
    EXPERIENCE = "experience"
    EDUCATION = "education"
    PROJECT = "project"
    CERTIFICATION = "certification"
    #: A drafted answer for a REVIEW_REQUIRED field, keyed by the classifier's match label.
    ANSWER = "answer"


class FactSource(str, Enum):
    MANUAL = "manual"
    CV_IMPORT = "cv_import"
    INFERRED = "inferred"


#: Categories whose facts map onto form fields by key (see policy.field_classifier).
#: Everything else is context for document generation rather than direct autofill.
AUTOFILL_CATEGORIES: frozenset[str] = frozenset(
    {FactCategory.IDENTITY.value, FactCategory.ANSWER.value}
)


def fact_identity(key: str, value: str, category: str) -> tuple[str, str, str]:
    """What makes two facts 'the same fact'.

    Identity facts are keyed: one first name, one email. Re-importing a CV should update
    the value in place rather than accumulating duplicates.

    List-like facts (skills, roles, projects) are keyed by their *value*, because a person
    has many of them and 'skill = Python' is a different fact from 'skill = Docker'.
    """
    normalised = " ".join(value.split()).strip().casefold()
    if category in AUTOFILL_CATEGORIES:
        return (category, key.strip().casefold(), "")
    return (category, key.strip().casefold(), normalised)
