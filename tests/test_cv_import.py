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
