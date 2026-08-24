"""Document import: files in, proposed facts out.

Nothing here writes to the profile. Extraction produces proposals; only an explicit accept
from the user turns one into a fact the agent may use.
"""

from .cv_parser import CVExtraction, CVParser, ExtractedFact, LLMCVParser, split_sections
from .extract import Extracted, ExtractionError, extract, sha256, sniff
from .generator import (
    DocumentGenerator,
    DocumentItem,
    DocumentPlan,
    DocumentSection,
    LLMDocumentGenerator,
    UngroundedDocument,
    assert_grounded,
)
from .matching import MatchResult, Requirement, extract_requirements, match
from .reconcile import Proposal, Verdict, reconcile, summarise
from .render import render_html, render_pdf

__all__ = [
    "CVExtraction",
    "DocumentGenerator",
    "DocumentItem",
    "DocumentPlan",
    "DocumentSection",
    "LLMDocumentGenerator",
    "MatchResult",
    "Requirement",
    "UngroundedDocument",
    "assert_grounded",
    "extract_requirements",
    "match",
    "render_html",
    "render_pdf",
    "CVParser",
    "Extracted",
    "ExtractedFact",
    "ExtractionError",
    "LLMCVParser",
    "Proposal",
    "Verdict",
    "extract",
    "reconcile",
    "sha256",
    "sniff",
    "split_sections",
    "summarise",
]
