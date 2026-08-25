"""Group skills the way a CV should present them, and reject things that are not skills.

Two problems this solves, both visible on a real generated CV:

  * A single undifferentiated row of fifteen terms. Recruiters skim; "Languages: Python,
    TypeScript" is read, "Python TypeScript OOP Docker Figma ..." is not.
  * Junk in the list. A CV whose "CS Fundamentals: Data Structures & Algorithms, OOP,
    Design Patterns" line is parsed naively ends up claiming "OOP" and "Data Structures &
    Algorithms" as skills, which reads as padding.

The groups follow ordinary engineering-CV convention: languages, then frameworks, then data
and AI, then tools. Anything unrecognised keeps its own group rather than being dropped --
the profile is the user's, and silently discarding a real skill would be worse than showing
it under a general heading.
"""

from __future__ import annotations

import re

#: Ordered: a reader scans top-down, and languages are what a technical screen looks for.
GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Languages",
        (
            "Python", "JavaScript", "TypeScript", "Java", "C#", "C++", "C", "Go", "Rust",
            "PHP", "Ruby", "Kotlin", "Swift", "Scala", "R", "MATLAB", "Dart", "Bash",
            "PowerShell", "SQL", "HTML", "CSS",
        ),
    ),
    (
        "Frameworks",
        (
            "React", "React Native", "Next.js", "Vue", "Angular", "Svelte", "Node.js",
            "Express", "Express.js", "Django", "Flask", "FastAPI", "Laravel", "Spring",
            "Rails", ".NET", "Tailwind", "Flutter", "Redux", "GraphQL", "REST",
        ),
    ),
    (
        "Data & AI",
        (
            "PostgreSQL", "MySQL", "SQLite", "MongoDB", "Redis", "Elasticsearch",
            "Cassandra", "NoSQL", "PyTorch", "TensorFlow", "scikit-learn", "Pandas",
            "NumPy", "Hugging Face", "LangChain", "LlamaIndex", "RAG", "LLM", "NLP", "OCR",
            "Computer Vision", "Machine Learning", "Deep Learning", "Vector search",
            "Embeddings", "Gemini API", "OpenAI",
        ),
    ),
    (
        "Tools & Platforms",
        (
            "Docker", "Kubernetes", "Terraform", "Ansible", "Jenkins", "GitHub Actions",
            "GitLab CI", "AWS", "Azure", "GCP", "Linux", "Nginx", "Kafka", "RabbitMQ",
            "Git", "GitHub", "Jira", "Figma", "Firebase", "Postman", "CI/CD",
            "Playwright", "Selenium", "Cypress", "Microservices",
        ),
    ),
)

_GROUP_OF: dict[str, str] = {
    skill.casefold(): name for name, skills in GROUPS for skill in skills
}

OTHER_GROUP = "Other"

#: Academic topics and generic competencies. Real knowledge, but listing them as "skills"
#: alongside Python reads as filler on an engineering CV, and they crowd out the
#: technologies a screener is actually looking for.
_NOT_A_SKILL = {
    "oop", "object oriented programming", "object-oriented programming",
    "data structures", "algorithms", "data structures & algorithms",
    "data structures and algorithms", "design patterns", "complexity analysis",
    "problem solving", "problem-solving", "teamwork", "communication", "leadership",
    "time management", "critical thinking", "attention to detail", "cs fundamentals",
    "computer science", "software engineering", "web development", "mobile development",
    "backend", "frontend", "full stack", "full-stack", "programming", "coding",
    "databases", "apis", "api", "testing", "debugging", "agile", "scrum",
}

#: A "skill" longer than this is a phrase -- "Python-based AI workflows", "LLM API
#: integration". Descriptive, but not a skill entry, and it makes the row unreadable.
MAX_SKILL_WORDS = 3
MAX_SKILL_CHARS = 26

_WORD_RE = re.compile(r"[A-Za-z0-9+#.]+")


def is_presentable(value: str) -> bool:
    """Is this worth printing in a CV skills row?"""
    text = " ".join(value.split()).strip(" .,;:")
    if not text:
        return False
    if text.casefold() in _NOT_A_SKILL:
        return False
    if len(text) > MAX_SKILL_CHARS:
        return False
    if len(_WORD_RE.findall(text)) > MAX_SKILL_WORDS:
        return False
    # A trailing generic noun turns a technology into a phrase: "OCR workflows",
    # "LLM API integration", "AI orchestration".
    return not re.search(
        r"\b(workflows?|integration|concepts?|orchestration|fundamentals?|"
        r"development|systems?|processing|extraction|automation|pipelines?|"
        r"strategies|methods?|engineering)$",
        text,
        re.IGNORECASE,
    )


def group_of(value: str) -> str:
    """Which CV group a skill belongs under.

    A CV writes related tools as one entry -- "Git/GitHub", "HTML/CSS" -- and an exact
    lookup misses every one of them. Splitting on the slash puts "Git/GitHub" beside Docker
    under Tools & Platforms instead of stranding it alone under "Other", which is what a
    reader notices. Parts that disagree keep the entry in "Other": "Python/React" belongs to
    no single group, and guessing one would be worse than saying nothing.
    """
    text = " ".join(value.split()).casefold()
    direct = _GROUP_OF.get(text)
    if direct is not None:
        return direct

    if "/" in text:
        groups = {_GROUP_OF.get(part.strip()) for part in text.split("/") if part.strip()}
        groups.discard(None)
        if len(groups) == 1:
            return groups.pop()

    return OTHER_GROUP


def group_skills(values: list[str]) -> list[tuple[str, list[str]]]:
    """Bucket skills into CV groups, preserving the order given within each group.

    Empty groups are omitted, and `Other` sorts last so an unrecognised skill never leads
    the section.
    """
    buckets: dict[str, list[str]] = {}
    seen: set[str] = set()

    for value in values:
        key = " ".join(value.split()).casefold()
        if key in seen:
            continue
        seen.add(key)
        buckets.setdefault(group_of(value), []).append(value)

    ordered = [(name, buckets[name]) for name, _ in GROUPS if name in buckets]
    if OTHER_GROUP in buckets:
        ordered.append((OTHER_GROUP, buckets[OTHER_GROUP]))
    return ordered
