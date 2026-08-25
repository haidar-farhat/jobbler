"""CV import: extraction, parsing, and the reconciliation that keeps it safe.

The property that matters most is negative: uploading a CV must not change what the agent is
willing to type into an application until a human has accepted each fact.
"""

from __future__ import annotations

import io
from pathlib import Path
from uuid import uuid4

import pytest
from localapply.documents.cv_parser import CVParser, split_sections
from localapply.documents.extract import ExtractionError, extract, sha256, sniff
from localapply.documents.reconcile import Verdict, reconcile, summarise
from localapply.profile.facts import FactCategory, FactStatus, fact_identity, is_usable

FIXTURE = Path(__file__).resolve().parents[1] / "evaluation" / "fixtures" / "sample-cv.txt"


@pytest.fixture
def cv_text() -> str:
    return FIXTURE.read_text(encoding="utf-8")


@pytest.fixture
def extraction(cv_text):
    return CVParser().parse(cv_text)


def facts_for(extraction, key: str):
    return [f for f in extraction.facts if f.key == key]


# --------------------------------------------------------------------------------------
# Extraction: bytes -> text
# --------------------------------------------------------------------------------------


def test_sniff_prefers_content_over_extension():
    """A mislabelled extension is common; a wrong parser gives confusing garbage."""
    assert sniff(b"%PDF-1.7 ...", "cv.docx") == "pdf"
    assert sniff(b"PK\x03\x04...", "cv.pdf") == "docx"
    assert sniff(b"Plain text CV", "cv.txt") == "text"


def test_extract_plain_text(cv_text):
    result = extract(cv_text.encode("utf-8"), "cv.txt")
    assert result.parser == "text"
    assert "Haidar Farhat" in result.text


def test_empty_file_is_an_explicit_error():
    with pytest.raises(ExtractionError, match="empty"):
        extract(b"", "cv.pdf")


def test_text_free_document_is_reported_not_silently_empty():
    """'We found no facts' and 'we could not read the file' are different messages.

    A scanned CV extracts to almost nothing; that must surface as a clear failure rather
    than an empty profile, since no OCR runs here.
    """
    with pytest.raises(ExtractionError, match="characters|scan"):
        extract(b"Haidar Farhat", "scan.txt")


def test_corrupt_pdf_reports_a_read_failure_not_a_content_problem():
    """Distinct from the case above: this file cannot be parsed at all, and saying
    'it looks like a scan' would send the user down the wrong path."""
    with pytest.raises(ExtractionError, match="Could not read this PDF"):
        extract(b"%PDF-1.4\n" + b"x" * 400, "broken.pdf")


def test_unsupported_type_is_refused():
    with pytest.raises(ExtractionError, match="Unsupported"):
        extract(b"\x89PNG\r\n\x1a\n" + b"\x00" * 400, "photo.png")


def test_sha256_is_stable():
    assert sha256(b"abc") == sha256(b"abc")
    assert sha256(b"abc") != sha256(b"abd")


def test_real_docx_round_trip():
    docx = pytest.importorskip("docx")
    document = docx.Document()
    for line in ("Haidar Farhat", "haidar@example.com", "SKILLS", "Python, FastAPI, Docker"):
        document.add_paragraph(line)
    document.add_paragraph("x" * 200)  # clear the min-useful-characters floor
    buffer = io.BytesIO()
    document.save(buffer)

    result = extract(buffer.getvalue(), "cv.docx")
    assert result.parser == "docx"
    assert "haidar@example.com" in result.text


# --------------------------------------------------------------------------------------
# Parsing: text -> facts
# --------------------------------------------------------------------------------------


def test_sections_are_recognised(cv_text):
    sections = split_sections(cv_text)
    for expected in ("header", "summary", "skills", "experience", "education", "projects"):
        assert expected in sections, f"missing section {expected}"


def test_contact_details_extracted(extraction):
    values = {f.key: f.value for f in extraction.facts}
    assert values["full_name"] == "Haidar Farhat"
    assert values["first_name"] == "Haidar"
    assert values["last_name"] == "Farhat"
    assert values["email"] == "haidar@example.com"
    assert "haidar-farhat" in values["linkedin_url"]
    assert "github.com/haidar-farhat" in values["github_url"]


def test_phone_extracted_without_matching_dates(extraction):
    phone = facts_for(extraction, "phone")
    assert phone, "phone should be found in the header"
    digits = sum(c.isdigit() for c in phone[0].value)
    assert digits >= 8
    # A year range must never be mistaken for a phone number.
    assert "2021" not in phone[0].value


def test_skills_extracted_from_the_skills_section(extraction):
    skills = {f.value.lower() for f in extraction.by_category(FactCategory.SKILL.value)}
    for expected in ("python", "fastapi", "docker", "playwright", "rag"):
        assert expected in skills, f"missing skill {expected}"


def test_experience_entries_are_split_by_date_range(extraction):
    experience = extraction.by_category(FactCategory.EXPERIENCE.value)
    assert len(experience) >= 2
    combined = " ".join(f.value for f in experience)
    assert "Fitly" in combined
    assert "CarePool" in combined


def test_education_and_certifications_extracted(extraction):
    education = extraction.by_category(FactCategory.EDUCATION.value)
    certs = extraction.by_category(FactCategory.CERTIFICATION.value)
    assert any("Lebanese University" in f.value for f in education)
    assert any("AWS" in f.value for f in certs)


def test_every_fact_carries_confidence_and_evidence(extraction):
    """A proposal you cannot check against the source is a proposal you cannot judge."""
    for fact in extraction.facts:
        assert 0.0 < fact.confidence <= 1.0
        assert fact.evidence, f"{fact.key} has no source line"


def test_parser_says_what_it_could_not_find():
    result = CVParser().parse("Some prose with no contact details and no headings at all.")
    assert result.warnings
    assert any("email" in w for w in result.warnings)


def test_parser_invents_nothing_from_an_empty_document():
    assert CVParser().parse("").facts == []


# --------------------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------------------


class FakeFact:
    """Stands in for db.models.ProfileFact without needing a database."""

    def __init__(self, key, value, category="identity", status=FactStatus.ACCEPTED.value):
        self.id = uuid4()
        self.key = key
        self.value = value
        self.category = category
        self.status = status


def test_new_facts_are_new(extraction):
    proposals = reconcile(extraction.facts, [])
    assert proposals
    assert all(p.verdict is Verdict.NEW for p in proposals)


def test_identical_existing_fact_is_a_duplicate(extraction):
    existing = [FakeFact("email", "haidar@example.com")]
    proposals = {p.fact.key: p for p in reconcile(extraction.facts, existing)}
    assert proposals["email"].verdict is Verdict.DUPLICATE
    assert not proposals["email"].actionable


def test_changed_value_is_a_conflict_that_names_what_it_replaces(extraction):
    existing = [FakeFact("email", "old-address@example.com")]
    proposal = {p.fact.key: p for p in reconcile(extraction.facts, existing)}["email"]

    assert proposal.verdict is Verdict.CONFLICT
    assert proposal.current_value == "old-address@example.com"
    assert proposal.supersedes_id == existing[0].id
    assert proposal.actionable


def test_a_rejected_value_is_not_proposed_again(extraction):
    existing = [
        FakeFact("email", "haidar@example.com", status=FactStatus.REJECTED.value)
    ]
    proposal = {p.fact.key: p for p in reconcile(extraction.facts, existing)}["email"]
    assert proposal.verdict is Verdict.DECLINED
    assert not proposal.actionable


def test_rejecting_one_value_still_lets_a_different_one_through():
    """Declining 'Python' must not silently swallow a later correction."""
    from localapply.documents.cv_parser import ExtractedFact

    existing = [FakeFact("email", "typo@example.com", status=FactStatus.REJECTED.value)]
    extracted = [ExtractedFact("email", "correct@example.com", "identity", 0.9, "line")]

    proposals = reconcile(extracted, existing)
    assert proposals[0].verdict is Verdict.NEW


def test_repeated_skill_in_one_document_is_proposed_once():
    from localapply.documents.cv_parser import ExtractedFact

    extracted = [
        ExtractedFact("Python", "Python", FactCategory.SKILL.value, 0.9, "a"),
        ExtractedFact("Python", "Python", FactCategory.SKILL.value, 0.7, "b"),
    ]
    assert len(reconcile(extracted, [])) == 1


def test_skills_are_identified_by_value_not_just_key():
    """A person has many skills, so 'Python' and 'Docker' are different facts -- unlike
    'email', of which there is one."""
    assert fact_identity("Python", "Python", FactCategory.SKILL.value) != fact_identity(
        "Docker", "Docker", FactCategory.SKILL.value
    )
    # Identity facts key on the field, so a new value is the *same* fact changing.
    assert fact_identity("email", "a@x.com", "identity") == fact_identity(
        "email", "b@x.com", "identity"
    )


def test_summary_counts_line_up(extraction):
    existing = [FakeFact("email", "haidar@example.com")]
    proposals = reconcile(extraction.facts, existing)
    counts = summarise(proposals)
    assert counts["duplicate"] == 1
    assert counts["actionable"] == sum(1 for p in proposals if p.actionable)


# --------------------------------------------------------------------------------------
# The safety property
# --------------------------------------------------------------------------------------


def test_only_accepted_status_is_usable():
    assert is_usable(FactStatus.ACCEPTED.value)
    for status in (FactStatus.PROPOSED, FactStatus.REJECTED, FactStatus.SUPERSEDED):
        assert not is_usable(status.value), f"{status.value} must not be usable"


# --------------------------------------------------------------------------------------
# Structured entries
#
# Reported from real use: the generated CV read as a list of database rows -- entries like
# "•Full-Stack Developer, Carepool Full-time | 1 Year · • Designed and deployed...". The
# extractor was joining raw lines with " · " and keeping the bullet glyphs.
# --------------------------------------------------------------------------------------


def test_bullet_glyphs_are_stripped():
    from localapply.documents.cv_parser import clean_line

    assert clean_line("•Full-Stack Developer") == "Full-Stack Developer"
    assert clean_line("  ●  Designed a thing  ") == "Designed a thing"
    assert clean_line("- Introduced CI/CD") == "Introduced CI/CD"
    # Real punctuation inside the line survives.
    assert clean_line("• Cut latency by 60%; owned the harness.") == (
        "Cut latency by 60%; owned the harness."
    )


def test_experience_is_split_into_role_organisation_and_dates(extraction):
    entries = extraction.by_category(FactCategory.EXPERIENCE.value)
    fitly = next(f for f in entries if "Fitly" in f.value)

    assert fitly.detail["role"] == "Senior AI Engineer"
    assert fitly.detail["organisation"] == "Fitly"
    assert "2023" in fitly.detail["dates"]
    assert "Present" in fitly.detail["dates"]


def test_experience_bullets_are_kept_separate(extraction):
    fitly = next(
        f for f in extraction.by_category(FactCategory.EXPERIENCE.value) if "Fitly" in f.value
    )
    bullets = fitly.detail["bullets"]

    assert len(bullets) >= 2
    assert any("RAG pipeline" in b for b in bullets)
    # The blob that started this: bullets must not be glued into the headline.
    assert "·" not in fitly.value
    assert "RAG pipeline" not in fitly.value


def test_employment_type_noise_is_dropped():
    from localapply.documents.cv_parser import _split_headline

    role, org, _dates = _split_headline("•Full-Stack Developer, Carepool Full-time | 1 Year")
    assert role == "Full-Stack Developer"
    assert "Full-time" not in org
    assert org.startswith("Carepool")


def test_headline_without_dates_still_parses():
    from localapply.documents.cv_parser import _split_headline

    role, org, dates = _split_headline("Backend Engineer at CarePool")
    assert role == "Backend Engineer"
    assert org == "CarePool"
    assert dates == ""


def test_projects_separate_title_from_description(extraction):
    projects = extraction.by_category(FactCategory.PROJECT.value)
    brevet = next(f for f in projects if "Brevet" in f.value)

    assert brevet.detail["title"] == "Brevet-GPT"
    assert "exam-preparation" in brevet.detail["description"]


def test_the_rendered_cv_is_not_a_row_dump(extraction):
    """End to end: structure reaches the page as a laid-out entry, not one joined string."""
    from uuid import uuid4

    from localapply.documents.generator import DocumentGenerator
    from localapply.documents.render import render_html
    from localapply.profile.facts import FactStatus

    class F:
        def __init__(self, f):
            self.id = uuid4()
            self.key, self.value, self.category = f.key, f.value, f.category
            self.detail = f.detail
            self.status = FactStatus.ACCEPTED.value

    html = render_html(DocumentGenerator().master_cv([F(f) for f in extraction.facts]))

    # A role renders as a laid-out entry, not one joined string.
    assert 'class="entry"' in html
    assert 'class="role"' in html
    assert "Senior AI Engineer" in html
    assert "2023 - Present" in html
    assert "<li>" in html, "bullets should be a real list, not run into the headline"

    body = html.split("</style>")[1]
    # No bullet glyphs or joined separators leaking into the rendered body.
    assert "•" not in body
    assert " · " not in body


# --------------------------------------------------------------------------------------
# Loose matching, and where it must stop
#
# Boundary-free skill matching exists because PDF extraction drops spaces: "Brevet GPTPython"
# hides "Python" from every word-boundary pattern. With no left boundary it also matched
# "git" inside "digital", and a real project line came out of the importer as two facts:
# "Built a di" and "gital payments-style platform with structured backend workflows".
# --------------------------------------------------------------------------------------


def test_a_skill_glued_to_a_project_name_is_still_split():
    """The case loose matching exists for. It must keep working."""
    from localapply.documents.cv_parser import _split_title_from_stack

    assert _split_title_from_stack("Brevet GPTPython, LLMs, Prompt Engineering") == (
        "Brevet GPT", "Python, LLMs, Prompt Engineering"
    )
    assert _split_title_from_stack("Safha JadidaAngular, PHP, MySQL") == (
        "Safha Jadida", "Angular, PHP, MySQL"
    )


def test_a_technology_name_hiding_inside_an_ordinary_word_is_not_a_split():
    """The regression. "digital" contains "git"; it is not a stack boundary."""
    from localapply.documents.cv_parser import _split_title_from_stack

    line = "Built a digital payments-style platform with structured backend workflows"
    assert _split_title_from_stack(line) == (line, "")


def test_prose_that_merely_names_a_technology_is_not_a_stack():
    """"LLM" here opens a sentence, not a technology list. Splitting on it produced a
    project called "Uses"."""
    from localapply.documents.cv_parser import _split_title_from_stack

    line = "Uses LLM-based prompting strategies to generate clear, curriculum-aligned answers"
    assert _split_title_from_stack(line) == (line, "")


def test_glue_seam_accepts_a_boundary_and_a_capitalised_join_only():
    import re

    from localapply.documents.cv_parser import at_glue_seam

    def seam(text, needle):
        return at_glue_seam(text, re.search(re.escape(needle), text))

    assert seam("EcoPay React, Laravel", "React"), "a plain word boundary"
    assert seam("Safha JadidaAngular", "Angular"), "glue keeps the name's own spelling"
    assert seam("Brevet GPTPython", "Python"), "glue after an acronym"
    assert not seam("Built a digital platform", "git"), "a coincidental lowercase substring"


def test_the_claim_checker_haystack_does_not_invent_a_skill_from_a_substring():
    """A false separation only ever makes the checker *more* permissive: it would accept a
    model claiming Git because the source happened to say "digital"."""
    from localapply.documents.claims import check_claims

    report = check_claims("Managed releases with Git.", ["Built a digital payments platform"])
    assert "Git" in report.unsupported_skills


def test_the_claim_checker_still_sees_a_glued_name():
    """The reason loose normalisation exists at all -- it must not be lost to the fix."""
    from localapply.documents.claims import check_claims

    report = check_claims(
        "Built services with Laravel and MySQL.", ["Engineered backend usingLaravelandMySQL"]
    )
    assert report.clean, report.describe()


# --------------------------------------------------------------------------------------
# PDF repair
#
# Untested until now, and the one module whose docstring records three rules that each
# corrupted real words before being removed. A rule here that misfires puts a mangled word
# on a CV, which is worse than the joined word it was trying to fix -- so the historical
# failures are pinned as tests, not just as prose.
# --------------------------------------------------------------------------------------


def test_repair_separates_a_joiner_word_from_a_technology():
    from localapply.documents.cleanup import repair

    assert "using Laravel" in repair("Engineered backend usingLaravel")
    assert "with Docker" in repair("Deployed withDocker")


def test_repair_adds_the_space_a_comma_lost():
    """"Engineered a cross-platform app using React,TypeScript, and React Native" came off a
    real CV and onto a generated one."""
    from localapply.documents.cleanup import repair

    assert repair("using React,TypeScript, and React Native") == (
        "using React, TypeScript, and React Native"
    )


def test_repair_leaves_a_thousands_separator_alone():
    from localapply.documents.cleanup import repair

    assert repair("Served 40,000 requests") == "Served 40,000 requests"


def test_repair_never_splits_a_word_that_merely_starts_with_a_joiner():
    """The removed rules turned "Instructor" into "In structor", "tooling" into "to oling",
    "MySQL" into "My SQL", and "TypeScript" into "Type Script"."""
    from localapply.documents.cleanup import repair

    for word in ("Instructor", "tooling", "MySQL", "TypeScript", "Information", "Together"):
        assert repair(word) == word


def test_repair_fixes_ligatures_and_hyphenated_line_breaks():
    from localapply.documents.cleanup import repair

    assert repair("CERTIﬁCATIONS") == "CERTIfiCATIONS"
    assert repair("perfor-\nmance") == "performance"


def test_repair_keeps_line_structure():
    """Section splitting depends on it; collapsing lines would take the headings with it."""
    from localapply.documents.cleanup import repair

    assert repair("EXPERIENCE\n\nRole, Company\n") == "EXPERIENCE\n\nRole, Company\n"
