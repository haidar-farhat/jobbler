"""Repair the damage PDF text extraction does, before anything tries to parse it.

Real CVs come out of `pypdf` with spaces missing, ligatures mangled and headings glued to
their content. Parsing that directly is what produced "51 skills and one experience blob"
from a CV containing six roles.

**Every rule here is conservative on purpose.** A first version had three clever generic
rules and all three corrupted real words:

  * a camel-case splitter turned "TypeScript" into "Type Script";
  * a "known skill glued to a lowercase word" rule turned "MySQL" into "My SQL";
  * a joiner-word rule compiled with `re.IGNORECASE`, which makes `[A-Z]` match lowercase
    too, so it split after any "in"/"to"/"and" prefix -- "Instructor" became "In structor"
    and "tooling" became "to oling".

Mangling a word is worse than leaving a word joined: a joined word is ugly, a mangled one is
wrong and ends up on a CV. So what survives is only what cannot misfire.
"""

from __future__ import annotations

import re

from .cv_parser import KNOWN_SKILLS

#: Ligatures and exotic spaces that PDF extraction emits raw or half-decoded
#: ("CERTIfiCATIONS").
_LIGATURES = {
    "ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl",
    " ": " ", "​": "", " ": " ", " ": " ",
}

#: Longest first, so "React Native" is considered before "React".
_SKILLS_BY_LENGTH = sorted(KNOWN_SKILLS, key=len, reverse=True)

#: Only these prefixes may be de-glued from a following technology name. Allowing *any*
#: lowercase run split "MySQL" into "My SQL"; a whitelist of joiner words cannot, because
#: "My" is not on it. Case is significant -- no IGNORECASE, which is what broke the earlier
#: version by making `[A-Z]` match lowercase.
_JOINERS = ("using", "with", "and", "for", "the", "via", "into", "from", "on", "in", "to")

_GLUED = [
    (
        skill,
        re.compile(
            r"\b(" + "|".join(_JOINERS) + r")(" + re.escape(skill) + r")\b"
        ),
    )
    for skill in _SKILLS_BY_LENGTH
]

#: "Development:RAG concepts" -> "Development: RAG concepts".
_TIGHT_COLON = re.compile(r"([A-Za-z)])\:(?=[A-Za-z(])")

#: "PHP ," -> "PHP,"
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([,;.)])")

#: "React,TypeScript" -> "React, TypeScript". Only before a capital: "1,000" and "Ph.D,ii"
#: keep their comma tight, and a lowercase continuation is far more likely to be a word the
#: extractor split than a missing space.
_TIGHT_COMMA = re.compile(r",(?=[A-Z])")

#: A word broken across a line by hyphenation: "perfor-\nmance" -> "performance".
_HYPHEN_BREAK = re.compile(r"([a-z])-\n([a-z])")

def _split_glued(text: str, skill: str, pattern: re.Pattern[str]) -> str:
    """Separate a joiner word from a technology name run into it: "usingLaravel"."""
    return pattern.sub(lambda m: f"{m.group(1)} {m.group(2)}", text)


def repair(text: str) -> str:
    """Undo extraction artefacts that cannot be mistaken for authorial intent."""
    if not text:
        return text

    for bad, good in _LIGATURES.items():
        text = text.replace(bad, good)

    text = _HYPHEN_BREAK.sub(r"\1\2", text)

    for skill, pattern in _GLUED:
        text = _split_glued(text, skill, pattern)

    text = _TIGHT_COLON.sub(r"\1: ", text)
    text = _TIGHT_COMMA.sub(", ", text)
    text = _SPACE_BEFORE_PUNCT.sub(r"\1", text)

    # Collapse runs of spaces without touching line structure, which parsing depends on.
    return "\n".join(re.sub(r"[ \t]{2,}", " ", line).rstrip() for line in text.split("\n"))
