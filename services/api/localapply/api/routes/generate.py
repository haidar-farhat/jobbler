"""Document generation: master CV, tailored CV, cover letter.

Every generation runs the same pipeline:

    accepted facts -> DocumentPlan -> assert_grounded -> HTML -> PDF -> stored version

`assert_grounded` is not optional and not configurable. If a plan claims anything your
accepted facts do not support, generation fails loudly instead of producing a document with
an invented line in it.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ...config import Settings
from ...db import models as m
from ...db.session import get_session
from ...documents.extract import sha256
from ...documents.generator import DocumentGenerator, UngroundedDocument, assert_grounded
from ...documents.render import render_html, render_pdf
from ...profile.facts import FactStatus
from ..deps import get_app_settings, get_router
from .profile import current_profile

router = APIRouter(prefix="/generate", tags=["documents"])


class GenerateIn(BaseModel):
    kind: str = "tailored_cv"  # master_cv | tailored_cv | cover_letter
    job_title: str | None = None
    company: str | None = None
    description: str = ""
    job_id: UUID | None = None
    #: Render a PDF as well as HTML. Costs a Chromium launch, so it is opt-out for previews.
    pdf: bool = True
    #: Run the full agentic pipeline: retrieve -> draft -> critique -> revise, with every
    #: produced line claim-checked. Needs a model; falls back to the rule-based document if
    #: one is not configured. See documents/writer.py.
    polish: bool = False


async def _accepted_facts(session: AsyncSession, profile_id: UUID) -> list[m.ProfileFact]:
    result = await session.execute(
        select(m.ProfileFact).where(
            m.ProfileFact.profile_id == profile_id,
            m.ProfileFact.status == FactStatus.ACCEPTED.value,
        )
    )
    return list(result.scalars().all())


async def _next_version(session: AsyncSession, profile_id: UUID, kind: str,
                        job_id: UUID | None) -> int:
    query = select(m.GeneratedDocument).where(
        m.GeneratedDocument.profile_id == profile_id, m.GeneratedDocument.kind == kind
    )
    query = query.where(
        m.GeneratedDocument.job_id == job_id if job_id else m.GeneratedDocument.job_id.is_(None)
    )
    existing = list((await session.execute(query)).scalars().all())
    return max((d.version for d in existing), default=0) + 1


@router.post("", status_code=201)
async def generate(
    payload: GenerateIn,
    settings: Settings = Depends(get_app_settings),
    router=Depends(get_router),
    session: AsyncSession = Depends(get_session),
) -> dict:
    profile = await current_profile(session)
    if profile is None:
        raise HTTPException(400, "Create a profile first.")

    facts = await _accepted_facts(session, profile.id)
    if not facts:
        raise HTTPException(
            400,
            "No accepted facts yet. Import a CV or add facts, and accept them, before "
            "generating a document.",
        )

    generator = DocumentGenerator()
    try:
        if payload.kind == "master_cv":
            plan = generator.master_cv(facts)
        elif payload.kind in {"tailored_cv", "cover_letter"}:
            if not payload.job_title:
                raise HTTPException(400, f"{payload.kind} needs a job_title.")
            builder = (
                generator.tailored_cv
                if payload.kind == "tailored_cv"
                else generator.cover_letter
            )
            plan = builder(
                facts,
                job_title=payload.job_title,
                company=payload.company,
                description=payload.description,
            )
        else:
            raise HTTPException(400, f"Unknown document kind {payload.kind!r}.")

        # The gate. Nothing renders until every line traces to an accepted fact.
        assert_grounded(plan, {f.id for f in facts})

    except UngroundedDocument as exc:
        raise HTTPException(422, str(exc)) from exc

    notes: list[str] = []
    write_report: dict = {}
    generator_name = generator.name

    if payload.polish and router is not None:
        if payload.kind == "tailored_cv":
            from ...documents.writer import write_tailored_cv

            plan, report = await write_tailored_cv(
                facts,
                job_title=payload.job_title,
                company=payload.company,
                description=payload.description,
                router=router,
                base_plan=plan,
                match=plan.match,
            )
            notes = report.notes
            write_report = report.as_dict()
            generator_name = "agentic"
        elif payload.kind == "cover_letter":
            from ...documents.writer import write_cover_letter

            plan, report = await write_cover_letter(
                facts,
                job_title=payload.job_title,
                company=payload.company,
                description=payload.description,
                router=router,
                base_plan=plan,
            )
            notes = report.notes
            write_report = report.as_dict()
            generator_name = "agentic"
        else:
            from ...documents.llm_writer import polish as polish_plan

            plan, notes = await polish_plan(
                plan, router, supporting_values=[f.value for f in facts]
            )
            generator_name = "rules+llm"

        # Whatever the model did, the finished plan must still cite only accepted facts.
        # Cheap to re-check, and the one assertion that catches a writer bug.
        assert_grounded(plan, {f.id for f in facts})

    markup = render_html(plan)
    version = await _next_version(session, profile.id, payload.kind, payload.job_id)

    document = m.GeneratedDocument(
        profile_id=profile.id,
        job_id=payload.job_id,
        kind=payload.kind,
        version=version,
        title=plan.title,
        job_title=plan.job_title,
        company=plan.company,
        html=markup,
        fact_ids=[str(fid) for fid in sorted(plan.fact_ids, key=str)],
        match_score=plan.match.score if plan.match else None,
        match_breakdown=plan.match.as_dict() if plan.match else {},
        generator=generator_name,
    )
    session.add(document)
    await session.commit()

    if payload.pdf:
        settings.ensure_dirs()
        path = settings.data_dir / "generated" / f"{document.id}.pdf"
        try:
            await render_pdf(plan, path)
            document.pdf_path = str(path)
            document.pdf_sha256 = sha256(path.read_bytes())
            session.add(document)
            await session.commit()
        except Exception as exc:  # noqa: BLE001 - HTML is still usable without a PDF
            document.pdf_path = None
            await session.commit()
            return {**_summary(document), "notes": notes, "writer": write_report,
                    "pdf_error": f"{exc.__class__.__name__}: {exc}"}

    return {**_summary(document), "notes": notes, "writer": write_report}


def _summary(document: m.GeneratedDocument) -> dict:
    return {
        "id": str(document.id),
        "kind": document.kind,
        "version": document.version,
        "title": document.title,
        "job_title": document.job_title,
        "company": document.company,
        "facts_used": len(document.fact_ids),
        "match_score": document.match_score,
        "match": document.match_breakdown,
        "has_pdf": bool(document.pdf_path),
        "generator": document.generator,
        "created_at": document.created_at.isoformat(),
    }


@router.get("")
async def list_generated(
    kind: str | None = None, session: AsyncSession = Depends(get_session)
) -> list[dict]:
    query = select(m.GeneratedDocument).order_by(m.GeneratedDocument.created_at.desc())
    if kind:
        query = query.where(m.GeneratedDocument.kind == kind)
    return [_summary(d) for d in (await session.execute(query)).scalars().all()]


@router.get("/{document_id}/preview", response_class=HTMLResponse)
async def preview(document_id: UUID, session: AsyncSession = Depends(get_session)):
    document = await session.get(m.GeneratedDocument, document_id)
    if document is None:
        raise HTTPException(404, "No such document.")
    return HTMLResponse(document.html)


@router.get("/{document_id}/pdf")
async def download_pdf(document_id: UUID, session: AsyncSession = Depends(get_session)):
    document = await session.get(m.GeneratedDocument, document_id)
    if document is None:
        raise HTTPException(404, "No such document.")
    if not document.pdf_path:
        raise HTTPException(404, "No PDF was rendered for this document.")

    from pathlib import Path

    path = Path(document.pdf_path)
    if not path.is_file():
        raise HTTPException(410, "The PDF file is missing from disk; regenerate it.")

    stem = (document.job_title or document.kind).replace(" ", "-").lower()
    return FileResponse(
        path, media_type="application/pdf",
        filename=f"{stem}-v{document.version}.pdf",
    )


@router.get("/{document_id}/provenance")
async def provenance(
    document_id: UUID, session: AsyncSession = Depends(get_session)
) -> dict:
    """Which accepted facts backed this document, and whether they still hold.

    A fact can be superseded after a document was sent. This shows that, rather than
    pretending the document still reflects the current profile.
    """
    document = await session.get(m.GeneratedDocument, document_id)
    if document is None:
        raise HTTPException(404, "No such document.")

    facts = []
    for raw in document.fact_ids:
        fact = await session.get(m.ProfileFact, UUID(raw))
        facts.append(
            {
                "id": raw,
                "key": fact.key if fact else None,
                "value": fact.value if fact else None,
                "category": fact.category if fact else None,
                "status_now": fact.status if fact else "deleted",
                "still_accepted": bool(fact and fact.status == FactStatus.ACCEPTED.value),
            }
        )
    stale = [f for f in facts if not f["still_accepted"]]
    return {
        "document": _summary(document),
        "facts": facts,
        "stale_facts": len(stale),
        "note": (
            "These facts have changed since this document was generated; it no longer "
            "reflects your current profile."
            if stale
            else "Every fact behind this document is still accepted."
        ),
    }
