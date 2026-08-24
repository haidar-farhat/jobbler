"""Check generated prose for claims the profile cannot back.

`assert_grounded` proves that every *item* in a document cites an accepted fact. That is
sufficient for the rule-based writers, which only ever emit a fact's own text. It is not
sufficient once a model writes the prose: a model can be handed five facts and still produce
a sentence mentioning a sixth thing.

So model-written text gets a second, narrower check: it must not name a technology the
profile has not accepted. That is the hallucination that matters here -- claiming a skill you
do not have is the difference between a tailored CV and a lie on a document with your name on
it.

This is a mitigation, not a proof. It catches named technologies from a known vocabulary; it
cannot catch "I led a team of forty". That is exactly why the model is only ever allowed to
*rephrase* a fixed set of facts, why the deterministic writer remains the default, and why
generation stays reviewable before anything is sent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .cv_parser import KNOWN_SKILLS, skill_pattern

_SKILL_PATTERNS = [(skill, skill_pattern(skill)) for skill in KNOWN_SKILLS]

#: Numbers a model likes to invent: team sizes, percentages, years.
_QUANTITY_RE = re.compile(
    r"\b(\d+)\s*(?:\+|plus)?\s*"
    r"(years?|months?|people|engineers?|developers?|users?|customers?|%|percent)\b",
    re.IGNORECASE,
)


class UnsupportedClaim(RuntimeError):
    """Generated prose asserted something the accepted facts do not support."""


@dataclass
class ClaimReport:
    unsupported_skills: list[str] = field(default_factory=list)
    unsupported_quantities: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.unsupported_skills and not self.unsupported_quantities

    def describe(self) -> str:
        parts = []
        if self.unsupported_skills:
            parts.append("skills not on your profile: " + ", ".join(self.unsupported_skills))
        if self.unsupported_quantities:
            parts.append(
                "figures that appear in no accepted fact: "
                + ", ".join(self.unsupported_quantities)
            )
        return "; ".join(parts)


def check_claims(text: str, supporting: list[str]) -> ClaimReport:
    """Compare generated prose against the text of the facts it was built from.

    `supporting` is the raw values of the accepted facts handed to the writer. Anything the
    prose names that those facts do not is reported.
    """
    haystack = " \n ".join(supporting).casefold()
    report = ClaimReport()

    for skill, pattern in _SKILL_PATTERNS:
        if pattern.search(text) and not pattern.search(haystack):
            report.unsupported_skills.append(skill)

    for match in _QUANTITY_RE.finditer(text):
        phrase = match.group(0)
        # The bare number appearing anywhere in the facts is enough; this is looking for
        # the invented figure with no source at all.
        if phrase.casefold() not in haystack and match.group(1) not in haystack:
            report.unsupported_quantities.append(phrase)

    return report


def assert_supported(text: str, supporting: list[str], *, where: str = "generated text") -> None:
    report = check_claims(text, supporting)
    if not report.clean:
        raise UnsupportedClaim(f"{where} contains {report.describe()}")
