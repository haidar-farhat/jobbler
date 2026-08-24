"""Document import: files in, proposed facts out.

Nothing here writes to the profile. Extraction produces proposals; only an explicit accept
from the user turns one into a fact the agent may use.
"""

from .cv_parser import CVExtraction, CVParser, ExtractedFact, LLMCVParser, split_sections
from .extract import Extracted, ExtractionError, extract, sha256, sniff
from .reconcile import Proposal, Verdict, reconcile, summarise

__all__ = [
    "CVExtraction",
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
