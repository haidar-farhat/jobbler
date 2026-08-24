"""Pick the facts that matter for one job.

The retrieval half of the writing pipeline. A profile holds a few dozen facts; a good CV
shows the handful that answer *this* posting and leaves the rest out. Choosing well is most
of what separates a tailored CV from a printout of a database.

Deliberately lexical rather than vector-based. At this corpus size -- tens of facts, not
millions -- an embedding index buys nothing but a model download and a similarity score you
cannot explain. Scoring on the vocabulary the job itself uses is transparent, instant, and
every choice can be shown to the user. The `Relevance.why` field exists for exactly that.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .cv_parser import KNOWN_SKILLS, skill_pattern

#: Words too common to signal anything, on top of the usual English stopwords.
_NOISE = {
    "the", "and", "for", "with", "you", "your", "our", "are", "will", "have", "has", "that",
    "this", "from", "was", "were", "been", "their", "they", "them", "who", "what", "which",
    "role", "team", "work", "working", "experience", "years", "year", "job", "position",
    "company", "candidate", "ability", "strong", "good", "great", "excellent", "must",
    "should", "would", "including", "using", "used", "use", "help", "build", "building",
    "please", "apply", "requirements", "responsibilities", "about", "plus", "nice",
}

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9+#.\-]{2,}")


def keywords(text: str) -> set[str]:
    """Content words from a job description, lowercased."""
    return {
        word.lower()
        for word in _WORD_RE.findall(text or "")
        if word.lower() not in _NOISE and len(word) > 2
    }


def skills_in(text: str) -> set[str]:
    """Named technologies from the known vocabulary, matched whole."""
    return {skill for skill in KNOWN_SKILLS if skill_pattern(skill).search(text or "")}


@dataclass
class Relevance:
    fact: object
    score: float = 0.0
    #: Which job terms this fact actually answers. Shown to the user, and given to the
    #: writer so it knows *why* an entry was chosen.
    matched_skills: list[str] = field(default_factory=list)
    matched_terms: list[str] = field(default_factory=list)

    @property
    def why(self) -> str:
        if self.matched_skills:
            return "answers " + ", ".join(self.matched_skills[:5])
        if self.matched_terms:
            return "mentions " + ", ".join(self.matched_terms[:4])
        return "background"


def _fact_text(fact) -> str:
    detail = getattr(fact, "detail", None) or {}
    parts = [fact.value, detail.get("role", ""), detail.get("organisation", "")]
    parts += detail.get("bullets", []) or []
    parts.append(detail.get("description", ""))
    return " ".join(p for p in parts if p)


def score_facts(facts: list, description: str) -> list[Relevance]:
    """Rank facts by how directly they speak to the job.

    A named technology the posting asks for is worth far more than an incidental word
    overlap, so skills dominate the score and loose term matches only break ties.
    """
    job_skills = skills_in(description)
    job_terms = keywords(description)

    ranked: list[Relevance] = []
    for fact in facts:
        text = _fact_text(fact)
        matched_skills = sorted(job_skills & skills_in(text))
        matched_terms = sorted((job_terms & keywords(text)) - {s.lower() for s in matched_skills})

        score = 3.0 * len(matched_skills) + 0.35 * min(len(matched_terms), 6)
        ranked.append(
            Relevance(
                fact=fact,
                score=score,
                matched_skills=matched_skills,
                matched_terms=matched_terms[:6],
            )
        )

    # Stable: equal scores keep the profile's own order, which is reverse-chronological.
    ranked.sort(key=lambda r: r.score, reverse=True)
    return ranked


def select_bullets(fact, description: str, limit: int = 3) -> list[str]:
    """Choose which of an entry's bullet points to show, best first.

    A role with eight bullets should show the three that answer the posting, not the first
    three the CV happened to list.
    """
    bullets = (getattr(fact, "detail", None) or {}).get("bullets") or []
    if len(bullets) <= limit:
        return list(bullets)

    job_skills = skills_in(description)
    job_terms = keywords(description)

    def rank(bullet: str) -> float:
        text_skills = skills_in(bullet)
        overlap = job_terms & keywords(bullet)
        # A bullet with a number in it is usually the one worth keeping.
        measurable = 1.0 if re.search(r"\d", bullet) else 0.0
        return 3.0 * len(job_skills & text_skills) + 0.3 * len(overlap) + measurable

    return sorted(bullets, key=rank, reverse=True)[:limit]


#: A CV showing twenty skills communicates less than one showing twelve, because the reader
#: cannot tell which matter. This is the cap on the rendered skills row.
MAX_SKILLS_SHOWN = 14


def _skill_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def curate_skills(skill_facts: list, description: str, limit: int = MAX_SKILLS_SHOWN) -> list:
    """Deduplicate and prioritise the skills a CV actually shows.

    Two jobs:

      * **Deduplicate near-identical entries.** A CV listing "CI", "CD" and "CI/CD" looks
        careless. Where one skill's name contains another's, the longer, more specific one
        wins and the fragment is dropped.
      * **Prioritise.** Skills the posting names come first, then the rest in profile order,
        truncated -- so the top of the row answers the job.

    Returns the surviving facts, ordered. Nothing is invented and nothing is renamed; this
    only chooses which accepted facts to show.
    """
    wanted = {s.lower() for s in skills_in(description)}

    kept: list = []
    for fact in skill_facts:
        key = _skill_key(fact.value)
        if not key:
            continue

        # Drop a fragment already covered by a longer name we are keeping ("CI" vs "CI/CD"),
        # unless the posting explicitly asks for the shorter one.
        redundant = any(
            key != _skill_key(other.value)
            and key in _skill_key(other.value)
            and fact.value.lower() not in wanted
            for other in skill_facts
        )
        if redundant or any(_skill_key(k.value) == key for k in kept):
            continue
        kept.append(fact)

    relevant = [f for f in kept if f.value.lower() in wanted]
    rest = [f for f in kept if f.value.lower() not in wanted]
    return (relevant + rest)[:limit]
