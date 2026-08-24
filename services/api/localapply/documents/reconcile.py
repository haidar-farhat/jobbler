"""Diff extracted facts against the profile you already have.

This is the step that makes CV import safe. The importer never writes to your profile; it
produces a set of *proposals*, each classified by what accepting it would do:

    NEW        nothing like it exists                  -> accepting adds it
    CONFLICT   an accepted fact has the same key,      -> accepting replaces it, and the old
               with a different value                     one is marked superseded
    DUPLICATE  you already have exactly this           -> nothing to do; not shown
    DECLINED   you rejected this exact value before    -> not shown again

Rejected facts are remembered precisely so a re-import does not ask you the same question
every time.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..profile.facts import FactStatus, fact_identity
from .cv_parser import ExtractedFact


class Verdict(str, Enum):
    NEW = "new"
    CONFLICT = "conflict"
    DUPLICATE = "duplicate"
    DECLINED = "declined"


@dataclass
class Proposal:
    fact: ExtractedFact
    verdict: Verdict
    #: The accepted fact this would replace, for CONFLICT.
    supersedes_id: object | None = None
    current_value: str | None = None

    @property
    def actionable(self) -> bool:
        """Whether this is worth putting in front of the user."""
        return self.verdict in {Verdict.NEW, Verdict.CONFLICT}


def _same_value(a: str, b: str) -> bool:
    return " ".join(a.split()).casefold() == " ".join(b.split()).casefold()


def reconcile(extracted: list[ExtractedFact], existing: list) -> list[Proposal]:
    """Classify each extracted fact against the facts already on the profile.

    `existing` is a list of `db.models.ProfileFact`. Only accepted and rejected facts carry
    weight: an outstanding proposal from a previous upload is replaced rather than compared
    against, so re-uploading a corrected CV supersedes the stale suggestion.
    """
    accepted_by_key: dict[tuple[str, str, str], list] = {}
    rejected: set[tuple[str, str, str]] = set()

    for row in existing:
        identity = fact_identity(row.key, row.value, row.category)
        if row.status == FactStatus.ACCEPTED.value:
            accepted_by_key.setdefault(identity, []).append(row)
        elif row.status == FactStatus.REJECTED.value:
            # Rejection is remembered by exact value, so a *changed* value still gets asked.
            rejected.add((row.category, row.key.casefold(), row.value.casefold()))

    proposals: list[Proposal] = []
    seen_in_this_document: set[tuple[str, str, str]] = set()

    for fact in extracted:
        identity = fact_identity(fact.key, fact.value, fact.category)

        # A CV that lists "Python" three times should ask once.
        if identity in seen_in_this_document:
            continue
        seen_in_this_document.add(identity)

        if (fact.category, fact.key.casefold(), fact.value.casefold()) in rejected:
            proposals.append(Proposal(fact, Verdict.DECLINED))
            continue

        matches = accepted_by_key.get(identity, [])
        if not matches:
            proposals.append(Proposal(fact, Verdict.NEW))
            continue

        current = matches[0]
        if _same_value(current.value, fact.value):
            proposals.append(Proposal(fact, Verdict.DUPLICATE, current_value=current.value))
        else:
            proposals.append(
                Proposal(fact, Verdict.CONFLICT,
                         supersedes_id=current.id, current_value=current.value)
            )

    return proposals


def summarise(proposals: list[Proposal]) -> dict[str, int]:
    counts = {v.value: 0 for v in Verdict}
    for proposal in proposals:
        counts[proposal.verdict.value] += 1
    counts["actionable"] = sum(1 for p in proposals if p.actionable)
    return counts
