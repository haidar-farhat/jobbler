"""Build one document, store it, render its PDF.

This is the body of `POST /generate` lifted out of the route, because it had already been
copied once -- `agent.py:_tailored_cv` grew its own version to attach a CV to a run, and that
copy drifted: it hardcoded `version=1` (so a second run for the same job silently overwrote
the first version number), and it never set `pdf_sha256` (so re-uploading a CV the app itself
generated was not recognised as the app's own output, which is the loop that poisoned a real
profile). The job pipeline would have been the third copy.

**It raises domain exceptions, never HTTPException.** A route may decide that no accepted
facts is a 400; the job pipeline decides it is a recoverable BLOCKED state you fix by
accepting facts. That is a caller's judgement, so the caller makes it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..db import models as m
from ..profile.store import accepted_facts
from .extract import sha256
from .generator import DocumentGenerator, UngroundedDocument, assert_grounded
from .render import render_html, render_pdf

#: Documents a job can have. `master_cv` is deliberately absent: it is the canonical record
#: of the whole profile and is not written for a posting.
JOB_DOCUMENT_KINDS = ("tailored_cv", "cover_letter")


class NoAcceptedFacts(RuntimeError):
    """Nothing to build from. Recoverable by accepting facts, so not an error state."""


@dataclass
class BuildResult:
    document: m.GeneratedDocument
    notes: list[str] = field(default_factory=list)
    write_report: dict = field(default_factory=dict)
    #: The document exists and its HTML is usable; only the PDF failed.
    pdf_error: str | None = None


async def build_document(
    session: AsyncSession,
    settings: Settings,
    profile: m.Profile,
    *,
    kind: str,
    job_title: str | None = None,
    company: str | None = None,
    description: str = "",
    job_id: UUID | None = None,
    pdf: bool = True,
    polish: bool = False,
    router=None,
) -> BuildResult:
    """Plan -> ground -> (optionally rewrite) -> ground again -> store -> render.

    `assert_grounded` runs before anything is stored and again after any model involvement.
    It is not optional and not configurable: a plan that claims something the accepted facts
    cannot back raises `UngroundedDocument` rather than producing a document with an invented
    line in it.
    """
    facts = await accepted_facts(session, profile.id)
    if not facts:
        raise NoAcceptedFacts(
            "No accepted facts yet. Import a CV or add facts, and accept them, before "
            "generating a document."
        )

    generator = DocumentGenerator()
    if kind == "master_cv":
        plan = generator.master_cv(facts)
    elif kind in JOB_DOCUMENT_KINDS:
        if not job_title:
            raise ValueError(f"{kind} needs a job_title.")
        builder = generator.tailored_cv if kind == "tailored_cv" else generator.cover_letter
        plan = builder(
            facts, job_title=job_title, company=company, description=description
        )
    else:
        raise ValueError(f"Unknown document kind {kind!r}.")

    accepted_ids = {f.id for f in facts}
    assert_grounded(plan, accepted_ids)

    notes: list[str] = []
    write_report: dict = {}
    generator_name = generator.name

    if polish and router is not None:
        plan, notes, write_report, generator_name = await _rewrite(
            plan, facts, router, kind=kind, job_title=job_title, company=company,
            description=description,
        )
        # Whatever the model did, the finished plan must still cite only accepted facts.
        assert_grounded(plan, accepted_ids)

    document = m.GeneratedDocument(
        profile_id=profile.id,
        job_id=job_id,
        kind=kind,
        version=await next_version(session, profile.id, kind, job_id),
        title=plan.title,
        job_title=plan.job_title,
        company=plan.company,
        html=render_html(plan),
        fact_ids=[str(fid) for fid in sorted(plan.fact_ids, key=str)],
        match_score=plan.match.score if plan.match else None,
        match_breakdown=plan.match.as_dict() if plan.match else {},
        generator=generator_name,
    )
    session.add(document)
    await session.commit()

    result = BuildResult(document=document, notes=notes, write_report=write_report)
    if not pdf:
        return result

    settings.ensure_dirs()
    path = settings.data_dir / "generated" / f"{document.id}.pdf"
    try:
        await render_pdf(plan, path)
    except Exception as exc:  # noqa: BLE001 - the HTML is still usable without a PDF
        document.pdf_path = None
        await session.commit()
        result.pdf_error = f"{exc.__class__.__name__}: {exc}"
        return result

    document.pdf_path = str(path)
    # The fingerprint of the app's own output. `documents.py` refuses an upload matching one,
    # which is what stops a generated CV being re-imported as source facts.
    document.pdf_sha256 = sha256(path.read_bytes())
    session.add(document)
    await session.commit()
    return result


async def _rewrite(plan, facts, router, *, kind, job_title, company, description):
    """Hand the plan to the model, within the boundary it cannot cross."""
    if kind == "tailored_cv":
        from .writer import write_tailored_cv

        plan, report = await write_tailored_cv(
            facts, job_title=job_title, company=company, description=description,
            router=router, base_plan=plan, match=plan.match,
        )
        return plan, report.notes, report.as_dict(), "agentic"

    if kind == "cover_letter":
        from .writer import write_cover_letter

        plan, report = await write_cover_letter(
            facts, job_title=job_title, company=company, description=description,
            router=router, base_plan=plan,
        )
        return plan, report.notes, report.as_dict(), "agentic"

    from .llm_writer import polish as polish_plan

    plan, notes = await polish_plan(
        plan, router, supporting_values=[f.value for f in facts]
    )
    return plan, notes, {}, "rules+llm"


async def next_version(
    session: AsyncSession, profile_id: UUID, kind: str, job_id: UUID | None
) -> int:
    """Versions increment and never overwrite, so what was actually sent stays recoverable."""
    from sqlmodel import select

    query = select(m.GeneratedDocument).where(
        m.GeneratedDocument.profile_id == profile_id, m.GeneratedDocument.kind == kind
    )
    query = query.where(
        m.GeneratedDocument.job_id == job_id
        if job_id
        else m.GeneratedDocument.job_id.is_(None)
    )
    existing = list((await session.execute(query)).scalars().all())
    return max((d.version for d in existing), default=0) + 1


__all__ = [
    "JOB_DOCUMENT_KINDS",
    "BuildResult",
    "NoAcceptedFacts",
    "UngroundedDocument",
    "build_document",
    "next_version",
]
