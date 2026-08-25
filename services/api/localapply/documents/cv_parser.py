"""CV text -> proposed facts.

Rule-based and deterministic, for the same reasons `StubReasoner` is: every test has a fixed
expected outcome, extraction can be reasoned about, and a bad CV cannot talk the parser into
inventing a qualification. `LLMCVParser` implements the same interface for later, and its
output lands in exactly the same review queue -- a model may propose, never accept.

Every fact carries a confidence and the source line it came from, so a proposal can be
checked against the document instead of taken on trust.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..profile.facts import FactCategory

# --------------------------------------------------------------------------------------
# Patterns
# --------------------------------------------------------------------------------------

EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]{2,}\b")
# Deliberately conservative: at least 8 digits, so dates and postcodes do not match.
PHONE_RE = re.compile(r"(?<!\w)(\+?\d[\d\s().-]{7,20}\d)(?!\w)")
LINKEDIN_RE = re.compile(r"(?:https?://)?(?:[\w-]+\.)?linkedin\.com/in/[\w%-]+/?", re.I)
GITHUB_RE = re.compile(r"(?:https?://)?(?:www\.)?github\.com/[\w-]+/?", re.I)
URL_RE = re.compile(r"https?://[^\s<>()\[\]]+", re.I)

#: Section headings, matched on a line of their own. Real CVs use headings this list will
#: never fully anticipate ("AI ENGINEERING & LLM SYSTEMS", "KEY ACHIEVEMENTS"), so
#: `looks_like_heading` also accepts any short ALL-CAPS line -- which is what CV headings
#: overwhelmingly are.
SECTION_RE = re.compile(
    r"^\s*(summary|profile|objective|about(?:\s+me)?|skills?|technical\s+skills?|"
    r"technologies|experience|work\s+experience|employment(?:\s+history)?|"
    r"professional\s+experience|education|qualifications|projects?|"
    r"certifications?|certificates|awards|languages|interests|references)"
    r"[\s&/-]*[a-z\s&/-]*:?\s*$",
    re.IGNORECASE,
)

SECTION_ALIASES = {
    "summary": "summary", "profile": "summary", "objective": "summary",
    "about": "summary", "about me": "summary",
    "skill": "skills", "skills": "skills", "technical skill": "skills",
    "technical skills": "skills", "technologies": "skills",
    "experience": "experience", "work experience": "experience",
    "employment": "experience", "employment history": "experience",
    "professional experience": "experience",
    "education": "education", "qualifications": "education",
    "project": "projects", "projects": "projects",
    "certification": "certifications", "certifications": "certifications",
    "certificates": "certifications",
    "awards": "awards", "languages": "languages",
    "interests": "interests", "references": "references",
}

#: An ALL-CAPS line short enough to be a heading rather than a sentence.
_CAPS_HEADING_RE = re.compile(r"^[A-Z][A-Z0-9 &/,'.\-]{2,44}$")

#: Words that identify which bucket an unrecognised heading belongs in.
_HEADING_HINTS: tuple[tuple[str, str], ...] = (
    ("achievement", "achievements"),
    ("experience", "experience"),
    ("employment", "experience"),
    ("education", "education"),
    ("certification", "certifications"),
    ("certificate", "certifications"),
    ("project", "projects"),
    ("skill", "skills"),
    ("technical", "skills"),
    ("technolog", "skills"),
    ("engineering", "skills"),
    ("stack", "skills"),
    ("summary", "summary"),
    ("profile", "summary"),
    ("language", "languages"),
    ("award", "awards"),
    ("interest", "interests"),
    ("reference", "references"),
)


def looks_like_heading(line: str) -> str | None:
    """Return the section a line introduces, or None.

    Two passes: the known vocabulary first, then any short ALL-CAPS line classified by the
    words it contains. Without the second pass, "EDUCATION & CERTIfiCATIONS" and "AI
    PROJECTS" fall into whatever section preceded them, and their content is lost.
    """
    stripped = line.strip().rstrip(":").strip()
    if not stripped or len(stripped) > 60:
        return None

    match = SECTION_RE.match(stripped)
    if match:
        key = re.sub(r"\s+", " ", match.group(1).strip().lower())
        return SECTION_ALIASES.get(key, key)

    # An ALL-CAPS line with no sentence punctuation is a heading in almost every CV.
    if _CAPS_HEADING_RE.match(stripped) and not stripped.endswith("."):
        lowered = stripped.lower()
        for hint, section in _HEADING_HINTS:
            if hint in lowered:
                return section
        return "other"
    return None


#: A curated vocabulary, so skills are recognised even without a SKILLS heading. Matching a
#: known list beats guessing at arbitrary capitalised words, which produces noise.
KNOWN_SKILLS: tuple[str, ...] = (
    "Python", "JavaScript", "TypeScript", "Java", "C#", "C++", "Go", "Rust", "PHP", "Ruby",
    "Kotlin", "Swift", "Scala", "R", "MATLAB", "Bash", "PowerShell", "SQL", "NoSQL",
    "React", "React Native", "Next.js", "Vue", "Angular", "Svelte", "Node.js", "Express",
    "Django", "Flask", "FastAPI", "Laravel", "Spring", "Rails", ".NET", "Tailwind",
    "PostgreSQL", "MySQL", "SQLite", "MongoDB", "Redis", "Elasticsearch", "Cassandra",
    "Docker", "Kubernetes", "Terraform", "Ansible", "Jenkins", "GitHub Actions", "GitLab CI",
    "AWS", "Azure", "GCP", "Linux", "Nginx", "Kafka", "RabbitMQ", "GraphQL", "REST",
    "PyTorch", "TensorFlow", "scikit-learn", "Pandas", "NumPy", "Hugging Face",
    "LangChain", "LlamaIndex", "RAG", "LLM", "NLP", "OCR", "Computer Vision",
    "Machine Learning", "Deep Learning", "Playwright", "Selenium", "Cypress",
    "Git", "Jira", "Figma", "Agile", "Scrum", "CI/CD", "Microservices", "Redux",
)

def skill_pattern(skill: str) -> re.Pattern[str]:
    """A word-boundary pattern for a technology name.

    The lookarounds exclude word characters, `+` and `#` so "Python" does not match
    "Pythonic" and "C" does not match "C++". A following dot is excluded only when it starts
    another word -- so "Node" will not match inside "Node.js", but a skill at the end of a
    sentence ("...experience with Kubernetes.") still matches. An earlier version excluded
    every following dot and silently missed exactly that case, which mattered most in the
    hallucination check.
    """
    escaped = re.escape(skill)
    return re.compile(
        r"(?<![\w+#])" + escaped + r"(?![\w+#])(?!\.\w)",
        re.IGNORECASE,
    )


_SKILL_PATTERNS = [(skill, skill_pattern(skill)) for skill in KNOWN_SKILLS]

#: Boundary-free, longest first, for finding where a technology list begins inside a line
#: that lost its spaces ("Brevet GPTPython, LLMs"). Names shorter than three characters are
#: excluded: the vocabulary contains "R" and "Go", and matching those without boundaries
#: would split on almost every word.
_LOOSE_SKILL_RE = re.compile(
    "|".join(
        re.escape(skill)
        for skill in sorted(KNOWN_SKILLS, key=len, reverse=True)
        if len(skill) >= 3
    ),
    re.IGNORECASE,
)

#: Exact spellings, for telling a glued name apart from a coincidental substring.
_CANONICAL_SKILLS = frozenset(KNOWN_SKILLS)


def at_glue_seam(text: str, match: re.Match[str]) -> bool:
    """Could a technology name really begin where this boundary-free match starts?

    Loose matching exists because PDF extraction drops spaces: "Brevet GPTPython" hides
    "Python" from any word-boundary pattern, and that is the whole case it handles. But a
    pattern with no left boundary also matches "git" inside "digital", which split a real
    project line into "Built a di" and "gital payments-style platform".

    The tell is capitalisation. Glue preserves a name's own spelling -- "GPTPython",
    "JadidaAngular" -- while a coincidental substring does not. So mid-word, only an
    exactly-spelled name counts; at a real word boundary, any casing does.
    """
    start = match.start()
    if start == 0 or not (text[start - 1].isalnum() or text[start - 1] == "_"):
        return True
    return match.group(0) in _CANONICAL_SKILLS

#: Split a skills line into individual skills. The slash is a separator only when spaced:
#: an unspaced one is usually part of the name ("CI/CD", "TCP/IP"), and splitting on it
#: produced three bogus entries -- "CI", "CD" and "CI/CD" -- on the rendered CV.
SKILL_SPLIT_RE = re.compile(r"[,;|•·●▪*\t]+|\s+/\s+|\s{3,}|\s+[-–—]\s+")

NAME_STOPWORDS = {
    "curriculum", "vitae", "resume", "cv", "profile", "contact", "summary", "objective",
    "personal", "details", "address", "phone", "email", "portfolio",
}

DATE_RANGE_RE = re.compile(
    r"((?:19|20)\d{2}|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*"
    r"(?:19|20)\d{2})\s*[-–—to]+\s*((?:19|20)\d{2}|present|current|now|"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*(?:19|20)\d{2})",
    re.IGNORECASE,
)

TITLE_HINT_RE = re.compile(
    r"\b(engineer|developer|designer|manager|analyst|consultant|architect|scientist|"
    r"intern|lead|director|specialist|administrator|researcher|founder|freelance)\b",
    re.IGNORECASE,
)


#: Bullet glyphs and list markers that PDF and DOCX extraction leaves in the text. Keeping
#: them turns a CV line into "•Full-Stack Developer, Carepool" on the rendered document.
BULLET_CHARS = "•·●▪◦‣∙*-–—•▪●"

#: Ceiling on skill proposals from one document. See _extract_skills.
MAX_SKILL_FACTS = 30


def clean_line(text: str) -> str:
    """Strip list markers and collapse whitespace, without touching real punctuation."""
    cleaned = text.strip().lstrip(BULLET_CHARS).strip()
    return re.sub(r"\s+", " ", cleaned)


@dataclass
class ExtractedFact:
    key: str
    value: str
    category: str
    confidence: float = 0.8
    evidence: str = ""
    #: Structured parts, for entries that have them: role, organisation, dates, bullets.
    #: The CV template renders these properly instead of printing one long joined line.
    detail: dict = field(default_factory=dict)


@dataclass
class CVExtraction:
    facts: list[ExtractedFact] = field(default_factory=list)
    sections: dict[str, list[str]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def by_category(self, category: str) -> list[ExtractedFact]:
        return [f for f in self.facts if f.category == category]


# --------------------------------------------------------------------------------------


def split_sections(text: str) -> dict[str, list[str]]:
    """Group lines under their heading. Anything before the first heading is 'header',
    which is where contact details almost always live."""
    sections: dict[str, list[str]] = {"header": []}
    current = "header"

    for raw in text.split("\n"):
        line = raw.strip()
        heading = looks_like_heading(line)
        if heading is not None:
            current = heading
            sections.setdefault(current, [])
            continue
        if line:
            sections.setdefault(current, []).append(line)

    return sections


def _looks_like_name(line: str) -> bool:
    if not (3 <= len(line) <= 60) or any(ch.isdigit() for ch in line):
        return False
    if "@" in line or "http" in line.lower() or "," in line:
        return False
    words = line.split()
    if not (2 <= len(words) <= 4):
        return False
    if any(w.lower().strip(".:") in NAME_STOPWORDS for w in words):
        return False
    # Accept "Haidar Farhat" and "HAIDAR FARHAT"; reject "senior software engineer".
    return all(w[:1].isupper() for w in words if w[:1].isalpha())


def find_name(sections: dict[str, list[str]], text: str) -> str | None:
    for line in sections.get("header", [])[:8]:
        if _looks_like_name(line):
            return " ".join(w.capitalize() if w.isupper() else w for w in line.split())
    # Fall back to the line immediately above the email address.
    match = EMAIL_RE.search(text)
    if match:
        preceding = text[: match.start()].split("\n")
        for line in reversed([ln.strip() for ln in preceding][-4:]):
            if _looks_like_name(line):
                return line
    return None


def _line_for(text: str, needle: str) -> str:
    for line in text.split("\n"):
        if needle in line:
            return line.strip()[:200]
    return ""


def _extract_contact(text: str, sections: dict[str, list[str]]) -> list[ExtractedFact]:
    facts: list[ExtractedFact] = []

    name = find_name(sections, text)
    if name:
        facts.append(ExtractedFact("full_name", name, FactCategory.IDENTITY.value, 0.85, name))
        parts = name.split()
        facts.append(
            ExtractedFact("first_name", parts[0], FactCategory.IDENTITY.value, 0.8, name)
        )
        if len(parts) > 1:
            facts.append(
                ExtractedFact("last_name", parts[-1], FactCategory.IDENTITY.value, 0.8, name)
            )

    email = EMAIL_RE.search(text)
    if email:
        facts.append(
            ExtractedFact(
                "email", email.group(0), FactCategory.IDENTITY.value, 0.97,
                _line_for(text, email.group(0)),
            )
        )

    # Search only the header for a phone: body text is full of digit runs.
    header = "\n".join(sections.get("header", []))
    phone = PHONE_RE.search(header)
    if phone:
        value = re.sub(r"\s{2,}", " ", phone.group(1)).strip()
        if sum(ch.isdigit() for ch in value) >= 8:
            facts.append(
                ExtractedFact("phone", value, FactCategory.IDENTITY.value, 0.85,
                              _line_for(text, phone.group(1)))
            )

    linkedin = LINKEDIN_RE.search(text)
    if linkedin:
        facts.append(
            ExtractedFact("linkedin_url", _as_url(linkedin.group(0)),
                          FactCategory.IDENTITY.value, 0.95, linkedin.group(0))
        )

    github = GITHUB_RE.search(text)
    if github:
        facts.append(
            ExtractedFact("github_url", _as_url(github.group(0)),
                          FactCategory.IDENTITY.value, 0.95, github.group(0))
        )

    for url in URL_RE.findall(text):
        if "linkedin.com" in url.lower() or "github.com" in url.lower():
            continue
        facts.append(
            ExtractedFact("portfolio_url", url.rstrip(".,);"),
                          FactCategory.IDENTITY.value, 0.6, url)
        )
        break

    return facts


def _as_url(value: str) -> str:
    value = value.rstrip("/")
    return value if value.lower().startswith("http") else f"https://{value}"


def _extract_skills(text: str, sections: dict[str, list[str]]) -> list[ExtractedFact]:
    facts: list[ExtractedFact] = []
    seen: set[str] = set()

    # Explicit SKILLS section: high confidence, because the author labelled it.
    for line in sections.get("skills", []):
        body = line.split(":", 1)[1] if ":" in line[:30] else line
        for token in SKILL_SPLIT_RE.split(body):
            skill = clean_line(token).strip(" .")
            if not (2 <= len(skill) <= 40) or skill.lower() in seen:
                continue
            if not any(ch.isalpha() for ch in skill):
                continue
            seen.add(skill.lower())
            facts.append(
                ExtractedFact(skill, skill, FactCategory.SKILL.value, 0.85, line[:200])
            )

    # A whole-document scan only when the CV has no usable skills section. When one exists,
    # scanning the prose as well produced 61 skills from a CV that lists about 30 -- every
    # technology mentioned in passing became a claimed skill, and the rendered CV turned
    # into a keyword wall.
    if len(facts) < 5:
        for skill, pattern in _SKILL_PATTERNS:
            if skill.lower() in seen:
                continue
            match = pattern.search(text)
            if match:
                seen.add(skill.lower())
                facts.append(
                    ExtractedFact(skill, skill, FactCategory.SKILL.value, 0.7,
                                  _line_for(text, match.group(0)))
                )

    # Filter BEFORE capping. Capping first let an early "AI ENGINEERING" section fill all
    # thirty slots with phrases ("RAG concepts", "AI orchestration"), which the CV then
    # discarded as unpresentable -- so a CV listing Python, TypeScript, React, Laravel,
    # MySQL and Docker rendered a skills row containing only "Python".
    from .taxonomy import is_presentable

    real = [f for f in facts if is_presentable(f.value)]
    dropped = [f for f in facts if not is_presentable(f.value)]

    # Keep a few of the descriptive ones at the end: they are genuine facts, useful for
    # matching and for a cover letter, just not CV skill entries.
    return (real + dropped)[:MAX_SKILL_FACTS]


#: A top-level bullet glued straight to its text -- "•Full-Stack Developer" -- which is how
#: PDF extraction renders the outer level of a nested list. The inner level keeps its space
#: ("• Designed and deployed..."). That difference is the most reliable entry delimiter in a
#: real CV, and far more common than a date range.
_ENTRY_BULLET_RE = re.compile(r"^[•·●▪‣∙]\S")
_SUB_BULLET_RE = re.compile(r"^[•·●▪‣∙*\-–—]\s")


def _is_entry_head(line: str) -> bool:
    """Does this line start a new entry (a role, a degree, a project)?

    Four signals, strongest first. Relying on date ranges alone -- the original rule --
    collapsed a CV with six roles into a single entry, because its roles are dated
    "Full-time | 1 Year" rather than "2021 - 2023".
    """
    stripped = line.strip()
    if not stripped:
        return False

    # 1. A bullet with no space after it: the outer level of a nested list.
    if _ENTRY_BULLET_RE.match(stripped):
        return True

    # 2. An inner bullet is never an entry head.
    if _SUB_BULLET_RE.match(stripped):
        return False

    body = clean_line(stripped)

    # 3. A dated line, the classic layout.
    if DATE_RANGE_RE.search(body):
        return True

    # 4. A short unbulleted line naming a role -- "Software Engineer, NU Scaler". Position
    #    relative to the previous line turned out not to add anything: an unbulleted role
    #    line is an entry head wherever it appears in an experience section.
    return bool(
        len(body) <= 90
        and TITLE_HINT_RE.search(body)
        and not body.endswith((".", ":"))
    )


def _entries(lines: list[str]) -> list[list[str]]:
    """Group a section's lines into entries.

    An entry is a head line plus the lines beneath it, so a role keeps its own bullets
    instead of every role in the section merging into one blob.
    """
    entries: list[list[str]] = []
    current: list[str] = []

    for line in lines:
        if _is_entry_head(line) and current:
            entries.append(current)
            current = [line]
        else:
            current.append(line)

    if current:
        entries.append(current)
    return [e for e in entries if e]


def _split_headline(headline: str) -> tuple[str, str, str]:
    """Pull role, organisation and dates out of an entry's first line.

    CV headlines are wildly inconsistent -- "Senior AI Engineer, Fitly - 2023 - Present",
    "Fitly | Senior AI Engineer | 2023", "Backend Engineer at CarePool". This handles the
    common shapes and degrades to putting everything in `role`, which still renders sensibly.
    """
    line = clean_line(headline)

    dates = ""
    match = DATE_RANGE_RE.search(line)
    if match:
        dates = clean_line(match.group(0))
        line = clean_line(line[: match.start()] + " " + line[match.end():])

    # Trailing separators left behind once the dates were removed.
    line = line.strip(" ,;|-–—·•")

    # Split at whichever separator appears *first*, not at whichever is first in a list of
    # preferences. "Full-Stack Developer, Carepool Full-time | 1 Year" splits at the comma;
    # preferring the pipe left role and employer glued together.
    role, organisation = line, ""
    earliest = None
    for separator in (" — ", " – ", " - ", " | ", ", ", " at ", " @ "):
        position = line.find(separator)
        if position != -1 and (earliest is None or position < earliest[0]):
            earliest = (position, separator)

    if earliest is not None:
        _, separator = earliest
        left, right = line.split(separator, 1)
        role, organisation = clean_line(left), clean_line(right)

    # Whatever follows a pipe or middot after the employer is duration or contract noise
    # ("| 1 Year"), not part of the name.
    organisation = re.split(r"\s*[|·•]\s*", organisation)[0]
    organisation = re.sub(
        r"\b(full[- ]time|part[- ]time|contract|freelance|internship|remote|"
        r"\d+\s*(?:year|month)s?)\b",
        "", organisation, flags=re.IGNORECASE,
    ).strip(" ,;|-–—")

    return role[:90], organisation[:90], dates[:40]


def _structured_entries(lines: list[str], category: str, limit: int,
                        confidence: float) -> list[ExtractedFact]:
    """One fact per entry, with its parts kept separate rather than joined into a blob."""
    facts: list[ExtractedFact] = []

    for entry in _entries(lines)[:limit]:
        cleaned = [clean_line(line) for line in entry if clean_line(line)]
        if not cleaned:
            continue

        role, organisation, dates = _split_headline(cleaned[0])
        bullets = [_trim_to_word(line, 190) for line in cleaned[1:] if len(line) > 12][:6]

        headline = " — ".join(p for p in (role, organisation) if p) or cleaned[0]
        facts.append(
            ExtractedFact(
                key=headline[:80],
                # `value` stays a readable one-liner, for the profile table and for any
                # consumer that does not understand `detail`.
                value=(f"{headline}{f' ({dates})' if dates else ''}")[:200],
                category=category,
                confidence=confidence if dates else confidence - 0.15,
                evidence=cleaned[0][:200],
                detail={
                    "role": role,
                    "organisation": organisation,
                    "dates": dates,
                    "bullets": bullets,
                },
            )
        )
    return facts


def _extract_experience(sections: dict[str, list[str]]) -> list[ExtractedFact]:
    return _structured_entries(
        sections.get("experience", []), FactCategory.EXPERIENCE.value, 12, 0.75
    )


def _extract_education(sections: dict[str, list[str]]) -> list[ExtractedFact]:
    return _structured_entries(
        sections.get("education", []), FactCategory.EDUCATION.value, 8, 0.7
    )


def _trim_to_word(text: str, limit: int) -> str:
    """Cut at a word boundary. Slicing mid-word left "...Brevet) curricu" on a real CV."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    return (cut[:space] if space > limit * 0.6 else cut).rstrip(" ,;.-") + "..."


#: A stack entry is a name, not a clause. Thirty characters covers "Prompt Engineering" and
#: "GitHub Actions" while excluding the sentence fragments that a mis-split produces.
_MAX_STACK_ITEM = 30


def _looks_like_a_stack(tail: str) -> bool:
    """Does this read as a comma-separated technology list rather than prose?"""
    parts = [p.strip() for p in SKILL_SPLIT_RE.split(tail) if p.strip()]
    return bool(parts) and all(len(p) <= _MAX_STACK_ITEM for p in parts)


def _split_title_from_stack(line: str) -> tuple[str, str]:
    """Separate a project name from the technology list run into it.

    "Brevet GPTPython, LLMs, Prompt Engineering" -> ("Brevet GPT", "Python, LLMs, Prompt
    Engineering"). The first known technology name marks where the stack begins.

    Two things have to hold before a match is believed, and both were learned from real
    output. The match must sit at a plausible seam (`at_glue_seam`), or "digital" splits
    into "di" + "gital". And what follows must actually read as a technology list rather
    than a sentence that happens to name one -- otherwise "Uses LLM-based prompting
    strategies to generate clear, curriculum-aligned explanations" becomes a project called
    "Uses".
    """
    # Loose matching, deliberately. The strict pattern requires a word boundary, so it
    # cannot see "Python" inside "GPTPython" -- which is the whole case being handled.
    for match in _LOOSE_SKILL_RE.finditer(line):
        # Only treat it as a stack if a real title precedes it.
        if match.start() < 3 or not at_glue_seam(line, match):
            continue
        tail = line[match.start():]
        if not _looks_like_a_stack(tail):
            continue
        return clean_line(line[: match.start()]).rstrip(" ,;-–—"), clean_line(tail)
    return clean_line(line), ""


def _extract_simple(sections: dict[str, list[str]], name: str, category: str,
                    confidence: float) -> list[ExtractedFact]:
    facts = []
    for raw in sections.get(name, [])[:12]:
        line = clean_line(raw)
        if len(line) <= 3:
            continue
        # "Brevet-GPT — an exam-preparation assistant" -> title and description apart, so
        # the CV can bold the name instead of printing one run-on line.
        title, _, description = line.partition(" — ")
        if not description:
            title, _, description = line.partition(" - ")

        # A project line often runs its name straight into its stack with no separator at
        # all -- "Brevet GPTPython, LLMs, Prompt Engineering" -- because the PDF lost the
        # space. Split at the first known technology name instead.
        stack = ""
        if not description:
            title, stack = _split_title_from_stack(line)
        facts.append(
            ExtractedFact(
                key=(title or line)[:80],
                value=line[:300],
                category=category,
                confidence=confidence,
                evidence=raw.strip()[:200],
                detail={
                    "title": clean_line(title or line)[:90],
                    "description": _trim_to_word(clean_line(description), 200),
                    "stack": stack[:90],
                },
            )
        )
    return facts


def _extract_title(sections: dict[str, list[str]]) -> list[ExtractedFact]:
    for line in sections.get("header", [])[:6]:
        if TITLE_HINT_RE.search(line) and len(line) <= 80 and "@" not in line:
            return [
                ExtractedFact("current_title", line.strip(" .-–—"),
                              FactCategory.IDENTITY.value, 0.7, line)
            ]
    for entry in _entries(sections.get("experience", []))[:1]:
        for line in entry[:2]:
            if TITLE_HINT_RE.search(line) and len(line) <= 80:
                cleaned = DATE_RANGE_RE.sub("", line).strip(" ,·|-–—")
                if cleaned:
                    return [
                        ExtractedFact("current_title", cleaned[:80],
                                      FactCategory.IDENTITY.value, 0.6, line)
                    ]
    return []


class CVParser:
    """Rule-based extraction. No model, no network, no randomness."""

    name = "rules"

    def parse(self, text: str) -> CVExtraction:
        # Repair PDF extraction artefacts first: without it, headings, glued words and
        # mangled ligatures make everything downstream guess.
        from .cleanup import repair

        text = repair(text)
        sections = split_sections(text)
        result = CVExtraction(sections=sections)

        result.facts.extend(_extract_contact(text, sections))
        result.facts.extend(_extract_title(sections))
        result.facts.extend(_extract_skills(text, sections))
        result.facts.extend(_extract_experience(sections))
        result.facts.extend(_extract_education(sections))
        result.facts.extend(
            _extract_simple(sections, "projects", FactCategory.PROJECT.value, 0.65)
        )
        result.facts.extend(
            _extract_simple(sections, "certifications",
                            FactCategory.CERTIFICATION.value, 0.75)
        )

        # Say what was not found, rather than leaving a silent gap in the profile.
        found = {f.key for f in result.facts}
        for key, label in (("email", "an email address"), ("full_name", "a name")):
            if key not in found:
                result.warnings.append(f"Could not find {label} in this document.")
        if not sections.get("experience"):
            result.warnings.append(
                "No experience section was recognised. If your CV uses an unusual heading, "
                "the entries will need adding by hand."
            )
        return result


class LLMCVParser(CVParser):
    """Phase 4. Same interface, a model behind it.

    Its output goes into the identical review queue: a model may propose facts, never accept
    them, so an extraction error stays a suggestion you decline rather than a lie in your CV.
    """

    name = "llm"

    def __init__(self, router) -> None:
        self._router = router

    def parse(self, text: str) -> CVExtraction:  # pragma: no cover - not wired yet
        raise NotImplementedError("Model-backed CV parsing lands with the AI engine.")
