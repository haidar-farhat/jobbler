"""Compose prose out of facts.

Used by the cover letter for every paragraph, and by the CV for its summary and for
putting roles in the order a reader expects.


The old letter dropped experience facts into the body verbatim, so a paragraph read:

    Full-Stack Developer — Carepool

That is a database row with a full stop missing, sitting between two sentences. It is the
same failure the CV had -- stating facts instead of writing a document -- and it cannot be
fixed in the template, because by the time the template runs the paragraph is already a
headline rather than a sentence.

So the work happens here: every paragraph is *composed*, from the structured parts of a
fact, into something a person would write. `role`, `organisation`, `dates` and `bullets`
become a sentence about a job; matched requirements become a sentence about fit. Nothing
is invented -- the sentence forms are fixed and only facts fill them -- but the output is
a letter rather than a list.

A model may then rewrite these paragraphs (documents/writer.py). It rewrites *prose*, which
is a job a model is good at; it never had to turn a row into a sentence, which is a job it
does by guessing.
"""

from __future__ import annotations

import re

#: Bullets are written to start with an action verb (see writer.BULLET_SYSTEM), which is
#: what lets one be folded into a sentence: "where I engineered backend services...".
#: A bullet starting with anything else -- a proper noun, a technology, a noun phrase --
#: is left as its own sentence instead of being lowercased into "react Native work...".
_ACTION_VERBS = frozenset({
    "architected", "authored", "automated", "built", "collaborated", "configured",
    "converted", "created", "cut", "delivered", "deployed", "designed", "developed",
    "diagnosed", "documented", "drove", "engineered", "established", "expanded",
    "generated", "grew", "handled", "implemented", "improved", "increased", "integrated",
    "introduced", "launched", "led", "maintained", "managed", "migrated", "modernised",
    "modernized", "optimised", "optimized", "orchestrated", "owned", "ported", "produced",
    "rebuilt", "reduced", "refactored", "released", "rewrote", "scaled", "shipped",
    "simplified", "standardised", "standardized", "streamlined", "supported", "tested",
    "trained", "translated", "tuned", "wrote",
})

_SENTENCE_END = ".!?"


def sentence(text: str) -> str:
    """Normalise whitespace and guarantee terminal punctuation."""
    clean = " ".join((text or "").split()).strip()
    if not clean:
        return ""
    return clean if clean[-1] in _SENTENCE_END else clean + "."


def oxford(items: list[str], conjunction: str = "and") -> str:
    """`[a, b, c]` -> "a, b and c". A comma-joined run reads like a form field.

    Two items that already contain "and" get a comma before the conjunction, so folded
    bullets do not come out as "engineered A and B and built C".
    """
    parts = [i for i in items if i]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2 and any(f" {conjunction} " in p for p in parts):
        return f"{parts[0]}, {conjunction} {parts[1]}"
    return f"{', '.join(parts[:-1])} {conjunction} {parts[-1]}"


#: Consonant letters whose *name* opens with a vowel sound, so an initialism starting with
#: one takes "an": an SRE, an ML engineer, an FPGA designer.
_VOWEL_SOUNDING_LETTERS = frozenset("AEFHILMNORSX")


def article(phrase: str) -> str:
    """"a" or "an", by sound rather than by spelling.

    Worth the twelve lines: "a AI Engineer" in the first sentence of a cover letter is the
    kind of error a reader stops reading at, and job titles are full of initialisms.
    """
    word = re.split(r"[^A-Za-z0-9]", (phrase or "").strip(), maxsplit=1)[0]
    if not word:
        return "a"
    if word.isupper() and len(word) > 1:
        return "an" if word[0] in _VOWEL_SOUNDING_LETTERS else "a"
    # "a university", "a European" -- spelled with a vowel, pronounced with a consonant.
    if re.match(r"(?i)^(uni|use|user|usual|eu|one|once)", word):
        return "a"
    return "an" if word[0].lower() in "aeiou" else "a"


#: A CV writes an open-ended range in whatever form it likes. Any of these at the end of a
#: date string means the role is current, which decides the tense of the whole sentence.
_STILL_THERE = re.compile(
    r"(?i)\b(present|current(?:ly)?|now|ongoing|today|to\s+date|date)\s*$"
)


def is_current(dates: str) -> bool:
    return bool(_STILL_THERE.search((dates or "").strip()))


_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


def end_year(dates: str) -> float | None:
    """The year a role ended, for ordering. `None` when the string holds no year at all.

    A role still held sorts above every dated one. A CV that writes "Full-time | 1 Year"
    gives up nothing, and returning None rather than guessing is what keeps those entries
    in the order the CV itself listed them instead of scattering them.
    """
    text = (dates or "").strip()
    if not text:
        return None
    if is_current(text):
        return float("inf")
    years = [int(m.group()) for m in _YEAR_RE.finditer(text)]
    return float(max(years)) if years else None


def chronological(entries: list) -> list:
    """Reverse-chronological, with undated entries kept in their original order at the end.

    A CV is read as a timeline. Ordering roles by relevance -- which is right for choosing
    *which* to show -- puts a job that ended two years ago above the one still held, and a
    reader takes that as an error rather than as emphasis. So relevance selects, and this
    orders.
    """
    def key(entry) -> tuple[int, float]:
        year = end_year((getattr(entry, "detail", None) or {}).get("dates", ""))
        return (1, 0.0) if year is None else (0, -year)

    return sorted(entries, key=key)


def profile_summary(
    current_title: str, top_skills: list[str], top_entry, job_title: str | None = None
) -> str:
    """The opening paragraph of a CV, composed rather than written.

    Without this, a CV generated with no model has no summary at all -- and the summary is
    the first thing both a recruiter and a screen reader hit. It restates the title, the
    skills that matched the posting, and the most relevant role; it concludes nothing, so
    there is nothing in it to be wrong.
    """
    headline = current_title or job_title or ""
    parts: list[str] = []

    if headline and top_skills:
        parts.append(
            f"{headline} working across {oxford(top_skills)}"
        )
    elif headline:
        parts.append(headline)
    elif top_skills:
        parts.append(f"Engineer working across {oxford(top_skills)}")

    if top_entry is not None:
        detail = getattr(top_entry, "detail", None) or {}
        role = (detail.get("role") or "").strip()
        organisation = (detail.get("organisation") or "").strip()
        dates = (detail.get("dates") or "").strip()
        if role and organisation:
            when = "Currently" if is_current(dates) else "Most recently"
            parts.append(f"{when} {article(role)} {role} at {organisation}")

    return sentence(". ".join(p for p in parts if p))


def _clause(bullet: str) -> str | None:
    """Turn a bullet into a subordinate clause, or return None if it will not fold.

    "Engineered backend services using Laravel." -> "engineered backend services using
    Laravel". Returning None is the honest answer for a bullet that is not verb-initial:
    forcing it produces "react Native app work", and a wrong sentence is worse than an
    extra one.
    """
    clean = " ".join((bullet or "").split()).strip().rstrip(".")
    if not clean:
        return None
    first = re.split(r"[^A-Za-z]", clean, maxsplit=1)[0]
    if first.casefold() not in _ACTION_VERBS:
        return None
    return clean[0].lower() + clean[1:]


def experience_paragraph(fact, bullets: list[str]) -> str:
    """One role, written as prose rather than printed as a row.

    Reads "At Carepool (2023 - Present) I work as a Full-Stack Developer, where I have
    engineered backend services and built a retrieval pipeline." Dates go in parentheses
    because a CV writes them in every imaginable form -- "2023 - Present", "1 Year",
    "Summer 2022" -- and parentheses are the one frame all of them survive.

    Tense follows the dates. Writing "I worked as" about the job someone still holds is a
    small error that reads as a big one.
    """
    detail = getattr(fact, "detail", None) or {}
    role = (detail.get("role") or "").strip()
    organisation = (detail.get("organisation") or "").strip()
    dates = (detail.get("dates") or "").strip()

    present = is_current(dates)
    verb = "work" if present else "worked"
    where = f" ({dates})" if dates else ""
    if role and organisation:
        head = f"At {organisation}{where} I {verb} as {article(role)} {role}"
    elif organisation:
        head = f"I {verb} at {organisation}{where}"
    elif role:
        head = f"I {verb} as {article(role)} {role}{where}"
    else:
        # No structure at all: the flat value is the best there is. Still a sentence.
        head = sentence(str(getattr(fact, "value", "")).strip())
        rest = " ".join(sentence(b) for b in bullets if b)
        return f"{head} {rest}".strip()

    clauses = [c for c in (_clause(b) for b in bullets) if c]
    leftovers = [b for b in bullets if _clause(b) is None]

    if clauses:
        # "where I have engineered" for a role still held, "where I engineered" for a past
        # one -- the perfect keeps a current role's achievements in the present.
        head += (", where I have " if present else ", where I ") + oxford(clauses)
    text = sentence(head)
    for bullet in leftovers:
        text += " " + sentence(bullet)
    return text


#: How many skills the opening names. The rest are held back for the fit paragraph, so the
#: letter does not list the same four technologies twice in five sentences.
OPENING_SKILLS = 2


def opening_paragraph(
    job_title: str, company: str | None, current_title: str, top_skills: list[str]
) -> str:
    """Why this letter exists, and who is writing it -- in two sentences, not four.

    The name is deliberately absent: it is already the letterhead and the signature, and a
    letter that introduces itself three times reads like a template.
    """
    where = f" at {company}" if company else ""
    text = f"I am writing to apply for the {job_title} position{where}."
    lead = top_skills[:OPENING_SKILLS]
    if current_title and lead:
        text += (
            f" I am {article(current_title)} {current_title}, and {oxford(lead)} "
            f"{'are' if len(lead) > 1 else 'is'} central to what I do."
        )
    elif current_title:
        text += f" I am {article(current_title)} {current_title}."
    elif lead:
        text += f" I work mainly in {oxford(lead)}."
    return text


def fit_paragraph(matched: list[str], missing: list[str]) -> str:
    """What else the posting asked for, and what is honestly not there.

    Only the requirements the opening did not already name appear here -- see
    `OPENING_SKILLS`. Missing requirements are named rather than quietly dropped: a letter
    listing only the hits is the one a reader stops trusting the moment they find the gap
    themselves, and naming it first is both more honest and, in a letter, more persuasive.
    """
    remaining = [s for s in matched if s not in matched[:OPENING_SKILLS]]
    parts = []
    if remaining:
        parts.append(
            f"The posting also asks for {oxford(remaining)}, "
            f"{'each of which is' if len(remaining) > 1 else 'which is also'} work I do."
        )
    if missing:
        parts.append(
            f"I have not worked with {oxford(missing, 'or')} directly, though I would "
            f"expect to pick {'them' if len(missing) > 1 else 'it'} up quickly."
        )
    return " ".join(parts)


def closing_paragraph(email: str, phone: str) -> str:
    reach = [c for c in (email, phone) if c]
    text = "I would welcome the chance to talk about the role in more detail."
    if reach:
        text += f" You can reach me at {oxford(reach, 'or')}."
    return text
