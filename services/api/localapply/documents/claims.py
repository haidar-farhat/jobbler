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

#: Boundary-free patterns, used only to normalise the *source* text for searching. Longest
#: first so "React Native" is separated before "React".
#:
#: Skills shorter than three characters are excluded. The vocabulary contains "R" and "Go",
#: and a boundary-free "R" rewrote every letter r in the text -- "services" became
#: "se R vices" -- which destroyed the haystack and made this check reject everything.
_MIN_LOOSE_LENGTH = 3

#: One alternation, longest first. A single pass cannot re-process its own output, whereas
#: substituting skill by skill does: " MySQL " was then split again by the shorter "SQL"
#: pattern into " My SQL ", losing the very name the step existed to expose.
_LOOSE_ALTERNATION = re.compile(
    "|".join(
        re.escape(skill)
        for skill in sorted(KNOWN_SKILLS, key=len, reverse=True)
        if len(skill) >= _MIN_LOOSE_LENGTH
    ),
    re.IGNORECASE,
)

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


def _searchable(texts: list[str]) -> str:
    """Normalise the source facts for *matching only*.

    PDF extraction glues words together -- "usingLaravelandMySQL" -- which hid Laravel and
    MySQL from this check and caused it to reject a rewrite that merely un-glued them. That
    is a false accusation of hallucination against a correct sentence.

    Splitting aggressively is safe here because the result is never displayed; it only
    decides whether a name appears in the source at all.
    """
    joined = " \n ".join(texts)
    # Loose patterns, deliberately: the strict one used for *detecting* a claim has word
    # boundaries, so it cannot see "Laravel" inside "usingLaravelandMySQL" either -- which is
    # exactly the text we are trying to make searchable.
    separated = _LOOSE_ALTERNATION.sub(lambda m: f" {m.group(0)} ", joined)

    # A generic lowercase-to-uppercase split used to run here too, and it undid the work
    # above: the freshly separated " MySQL " was split again into " My SQL ", losing the
    # name this step exists to expose. The alternation already handles every name in the
    # vocabulary, which is the only thing being searched for.
    #
    # Both forms are kept. Searching the original as well means normalisation can never
    # lose a name, only add ways to find one.
    return (separated + " \n " + joined).casefold()


def check_claims(text: str, supporting: list[str]) -> ClaimReport:
    """Compare generated prose against the text of the facts it was built from.

    `supporting` is the raw values of the accepted facts handed to the writer. Anything the
    prose names that those facts do not is reported.
    """
    haystack = _searchable(supporting)
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
