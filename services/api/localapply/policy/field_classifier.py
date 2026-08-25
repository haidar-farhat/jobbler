"""Classify a form field by how dangerous it is to fill automatically.

Three classes, and the **default for anything unrecognised is REVIEW_REQUIRED**, not
SAFE_AUTOFILL. An unknown field on an unknown site is exactly the case where guessing is
worst, so the system fails towards asking the human.

This is deliberately pattern matching over field names rather than an LLM call. It is the
security-relevant path: it must be deterministic, auditable, unit-testable in microseconds,
and immune to anything written on the page.
"""

from __future__ import annotations

import re
from enum import Enum

from ..contracts import ElementRole, ObservedElement


class FieldClass(str, Enum):
    SAFE_AUTOFILL = "safe_autofill"
    REVIEW_REQUIRED = "review_required"
    NEVER_AUTOFILL = "never_autofill"


class Classification:
    __slots__ = ("field_class", "matched", "profile_key")

    def __init__(
        self, field_class: FieldClass, matched: str, profile_key: str | None = None
    ) -> None:
        self.field_class = field_class
        self.matched = matched
        #: Which profile fact may fill this, when SAFE_AUTOFILL.
        self.profile_key = profile_key

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Classification {self.field_class.value} via {self.matched!r}>"


def _p(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


# --------------------------------------------------------------------------------------
# NEVER_AUTOFILL — legally significant, protected, or identity-sensitive.
# The agent must not touch these even with user approval; the human fills them in person.
# --------------------------------------------------------------------------------------
NEVER_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (_p(r"\b(e-?)?signature\b|\bsign here\b|\binitials?\b"), "electronic signature"),
    (_p(r"\bssn\b|social security|national insurance|\bnino\b|tax\s*id"), "government id"),
    (_p(r"criminal|conviction|felony|misdemeanou?r|background check"), "criminal history"),
    (_p(r"disability|disabilit|\bveteran\b|protected veteran"), "protected characteristic"),
    (_p(r"\brace\b|ethnicit|\bgender\b|\bsex\b\s*:?$|sexual orientation"), "demographic (EEO)"),
    (_p(r"date of birth|\bdob\b|\bbirth\s*date\b|\bage\b\s*:?$"), "date of birth"),
    (_p(r"password|passcode|\botp\b|verification code|2fa|two.?factor"), "credential"),
    (_p(r"credit card|card number|\bcvv\b|\biban\b|routing number|bank account"), "financial"),
    (_p(r"passport|driver'?s? licen[cs]e|visa number"), "identity document"),
    (_p(r"\bi certify\b|\bi attest\b|under penalty of perjury"), "legal attestation"),
]

# --------------------------------------------------------------------------------------
# REVIEW_REQUIRED — the agent may draft a value, but a human confirms before it is entered.
# These are negotiating positions and factual claims with real consequences.
# --------------------------------------------------------------------------------------
REVIEW_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (_p(r"salary|compensation|expected pay|desired pay|rate expectation|\bctc\b"), "salary"),
    (_p(r"sponsor|work authori[sz]|right to work|legally authori[sz]ed|require.*visa"),
     "work authorisation"),
    (_p(r"relocat|willing to move"), "relocation"),
    (_p(r"notice period|available (to )?start|start date|earliest availability"), "availability"),
    (_p(r"years? of experience|how many years"), "experience claim"),
    (_p(r"why (do you|are you)|tell us|describe|cover letter|motivat"), "free-text narrative"),
    (_p(r"referen(ce|ces|ee)"), "references"),
    (_p(r"current employer|currently employed|reason for leaving"), "employment detail"),
    (_p(r"\bnotice\b|\bconsent\b|\bagree\b|terms and conditions|privacy policy"), "consent"),
]

# --------------------------------------------------------------------------------------
# SAFE_AUTOFILL — verified, factual, already public on the CV. Mapped to a profile key.
# --------------------------------------------------------------------------------------
SAFE_PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    (_p(r"(first|given|fore)\s*name"), "first name", "first_name"),
    (_p(r"(last|family|sur)\s*name"), "last name", "last_name"),
    (_p(r"full name|^name$|your name|legal name"), "full name", "full_name"),
    (_p(r"e-?mail"), "email", "email"),
    (_p(r"phone|mobile|telephone|contact number"), "phone", "phone"),
    (_p(r"linked-?in"), "linkedin", "linkedin_url"),
    (_p(r"github|git hub"), "github", "github_url"),
    (_p(r"portfolio|personal (web)?site|website|\burl\b"), "portfolio", "portfolio_url"),
    (_p(r"\bcity\b|\btown\b"), "city", "city"),
    (_p(r"\bcountry\b"), "country", "country"),
    (_p(r"\baddress\b|street"), "address", "address"),
    (_p(r"post(al)? code|\bzip\b"), "postal code", "postal_code"),
    (_p(r"resume|\bcv\b|upload.*(resume|cv)"), "resume upload", "resume_path"),
    (_p(r"current (job )?title|job title|current role"), "job title", "current_title"),
]


#: Roles a value can actually be put into. Everything else -- a link, a button, a heading --
#: cannot be filled, so calling it "safe to autofill" is meaningless at best.
FILLABLE: frozenset[ElementRole] = frozenset({
    ElementRole.TEXTBOX,
    ElementRole.TEXTAREA,
    ElementRole.COMBOBOX,
    ElementRole.CHECKBOX,
    ElementRole.RADIO,
    ElementRole.FILE_INPUT,
})

#: Every label a SAFE pattern can match is short -- "First name", "Email", "Postal code",
#: "Current city of residence" is 25. Forty is roomy for all of them and excludes the
#: sentences that caused the trouble: "Which office would you like to work from? (New York
#: City, London, Berlin)" is 72.
MAX_LABEL_CHARS = 40

#: A label is a noun phrase. Past this it is a question or a sentence, and the thing it
#: happens to contain is not what the field is for.
MAX_LABEL_WORDS = 7


def _looks_like_a_label(name: str) -> bool:
    """Is this a field's label, or a sentence that happens to contain the word?

    Observed on a real Greenhouse board: a link reading "Director, Major Sales / Hybrid -
    New York City" matched `\\bcity\\b` and classified as SAFE_AUTOFILL with
    `profile_key=city`. On a listing page that is merely wrong. On a form it is the failure
    that matters -- a select labelled "Which office would you like to work from? (New York
    City, London, Berlin)" would have been filled with the candidate's home city, without
    anyone approving it.

    Whitespace is collapsed rather than rejected: a label broken across two lines in the
    markup is still a label, and treating "First\\nname" as page text would send a perfectly
    ordinary field to a human for approval.

    The asymmetry is deliberate and load-bearing: failing this check disqualifies a string
    from SAFE_AUTOFILL only. NEVER and REVIEW still apply to it, because erring towards
    asking a person is always allowed and erring towards filling automatically is not.
    """
    collapsed = " ".join((name or "").split())
    return (
        bool(collapsed)
        and len(collapsed) <= MAX_LABEL_CHARS
        and len(collapsed.split()) <= MAX_LABEL_WORDS
    )


def classify(element: ObservedElement) -> Classification:
    """Classify a single observed form element.

    Matching runs NEVER -> REVIEW -> SAFE, so the most restrictive class always wins when a
    label matches more than one pattern (e.g. "Salary expectation signature").
    """
    # Whitespace-collapsed, so a label broken across two lines in the markup matches the
    # same patterns as one that is not.
    haystack = " ".join(
        " ".join(part.split())
        for part in filter(None, [element.name, element.input_type, element.value or ""])
    )

    for pattern, label in NEVER_PATTERNS:
        if pattern.search(haystack):
            return Classification(FieldClass.NEVER_AUTOFILL, label)

    # An input the browser itself marks as a password is never safe, whatever it is called.
    if element.input_type == "password":
        return Classification(FieldClass.NEVER_AUTOFILL, "input[type=password]")

    for pattern, label in REVIEW_PATTERNS:
        if pattern.search(haystack):
            return Classification(FieldClass.REVIEW_REQUIRED, label)

    # SAFE is the only class that authorises acting without a human, so it is the only one
    # with entry conditions: the element must be something a value can go into, and its name
    # must read as a label rather than as page text.
    if element.role in FILLABLE and _looks_like_a_label(element.name):
        for pattern, label, profile_key in SAFE_PATTERNS:
            if pattern.search(haystack):
                return Classification(FieldClass.SAFE_AUTOFILL, label, profile_key)

    # Free-text areas are open-ended by nature; never autofill one we could not identify.
    if element.role is ElementRole.TEXTAREA:
        return Classification(FieldClass.REVIEW_REQUIRED, "unidentified free-text field")

    # Fail safe: unknown means ask.
    return Classification(FieldClass.REVIEW_REQUIRED, "unrecognised field")
