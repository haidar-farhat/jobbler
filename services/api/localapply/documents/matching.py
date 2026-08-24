"""Match a job's requirements against the profile.

Two jobs in one place, and both feed document generation:

  * pull the *stated* requirements out of a job description;
  * score them against **accepted** facts only.

The scoring is deliberately pessimistic. A skill the profile cannot evidence counts as
missing, never as a maybe -- because the output of this module decides what a generated CV
is allowed to claim, and an optimistic match here becomes a lie on a document with your name
on it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .cv_parser import KNOWN_SKILLS

_SKILL_PATTERNS = [
    (skill, re.compile(r"(?<![\w+#.])" + re.escape(skill) + r"(?![\w+#.])", re.IGNORECASE))
    for skill in KNOWN_SKILLS
]

#: Wording that marks the *following* requirements as optional.
_OPTIONAL_MARKER = re.compile(
    r"\b(nice[- ]to[- ]have|bonus|desirable|preferred|plus|advantageous|"
    r"would be (?:a )?plus|optional)\b",
    re.IGNORECASE,
)

_YEARS_RE = re.compile(r"(\d+)\+?\s*(?:\+|or more)?\s*years?", re.IGNORECASE)


@dataclass(frozen=True)
class Requirement:
    skill: str
    required: bool = True
    evidence: str = ""


@dataclass
class MatchResult:
    score: float = 0.0
    matched_required: list[str] = field(default_factory=list)
    missing_required: list[str] = field(default_factory=list)
    matched_optional: list[str] = field(default_factory=list)
    missing_optional: list[str] = field(default_factory=list)
    years_asked: int | None = None

    @property
    def matched(self) -> list[str]:
        return self.matched_required + self.matched_optional

    @property
    def recommendation(self) -> str:
        if self.score >= 0.8:
            return "apply"
        if self.score >= 0.55:
            return "consider"
        return "skip"

    def as_dict(self) -> dict:
        return {
            "score": round(self.score, 3),
            "matched_required": self.matched_required,
            "missing_required": self.missing_required,
            "matched_optional": self.matched_optional,
            "missing_optional": self.missing_optional,
            "years_asked": self.years_asked,
            "recommendation": self.recommendation,
        }


def extract_requirements(description: str) -> list[Requirement]:
    """Find named technologies in a job description, split by required vs nice-to-have.

    Only the known vocabulary is recognised. Guessing at arbitrary capitalised words
    produces noise, and noise here would end up shaping a CV.
    """
    if not description:
        return []

    lines = description.split("\n")
    optional_from: int | None = None
    for index, line in enumerate(lines):
        if _OPTIONAL_MARKER.search(line):
            optional_from = index
            break

    found: dict[str, Requirement] = {}
    for index, line in enumerate(lines):
        optional = optional_from is not None and index >= optional_from
        for skill, pattern in _SKILL_PATTERNS:
            if pattern.search(line) and skill not in found:
                found[skill] = Requirement(
                    skill=skill, required=not optional, evidence=line.strip()[:200]
                )
    return list(found.values())


def years_requested(description: str) -> int | None:
    """The largest explicit year count asked for, if any."""
    years = [int(m.group(1)) for m in _YEARS_RE.finditer(description or "")]
    return max(years) if years else None


def match(
    requirements: list[Requirement],
    profile_skills: set[str],
    description: str = "",
) -> MatchResult:
    """Score requirements against the skills the profile can actually evidence.

    `profile_skills` must come from accepted facts. Passing proposals in here would let an
    unreviewed extraction decide what a CV claims.
    """
    owned = {s.strip().casefold() for s in profile_skills}

    result = MatchResult(years_asked=years_requested(description))
    for requirement in requirements:
        has = requirement.skill.casefold() in owned
        if requirement.required:
            (result.matched_required if has else result.missing_required).append(
                requirement.skill
            )
        else:
            (result.matched_optional if has else result.missing_optional).append(
                requirement.skill
            )

    required_total = len(result.matched_required) + len(result.missing_required)
    optional_total = len(result.matched_optional) + len(result.missing_optional)

    if required_total == 0 and optional_total == 0:
        # Nothing recognised: report zero rather than a flattering 100%.
        result.score = 0.0
        return result

    required_score = (
        len(result.matched_required) / required_total if required_total else 1.0
    )
    optional_score = (
        len(result.matched_optional) / optional_total if optional_total else 1.0
    )
    # Required requirements dominate; nice-to-haves nudge.
    result.score = round(0.85 * required_score + 0.15 * optional_score, 3)
    return result


def rank_experience(experiences: list, matched_skills: list[str]) -> list:
    """Order experience facts by how much they evidence what the job asked for.

    Stable: entries mentioning more matched skills come first, and ties keep their original
    (reverse-chronological, as parsed) order.
    """
    lowered = [s.casefold() for s in matched_skills]

    def relevance(fact) -> int:
        haystack = f"{fact.key} {fact.value}".casefold()
        return sum(1 for skill in lowered if skill in haystack)

    return sorted(experiences, key=relevance, reverse=True)


def order_skills(profile_skills: list[str], matched: list[str]) -> list[str]:
    """Put the skills the job actually asked for first, keeping the rest in place."""
    wanted = {s.casefold() for s in matched}
    front = [s for s in profile_skills if s.casefold() in wanted]
    rest = [s for s in profile_skills if s.casefold() not in wanted]
    return front + rest
