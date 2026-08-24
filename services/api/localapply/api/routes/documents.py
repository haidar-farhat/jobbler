"""CV upload and the fact-review queue.

The flow is deliberately two-step:

    POST /documents            upload -> extract -> reconcile -> write PROPOSED facts
    GET  /documents/{id}/facts see what it found, with confidence and source line
    POST /profile/facts/{id}/accept | /reject   decide, one fact at a time

Uploading never changes your profile. That is the whole point: an extraction error is a
suggestion you decline, not a lie sitting in your CV.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ...config import Settings
from ...db import models as m
from ...db.models import utc_now
from ...db.session import get_session
from ...documents.cv_parser import CVParser
from ...documents.extract import ExtractionError, extract, sha256
from ...documents.reconcile import Verdict, reconcile, summarise
from ...profile.facts import FactSource, FactStatus
from ..deps import get_app_settings
from .profile import current_profile

router = APIRouter(prefix="/documents", tags=["documents"])

#: Generous for a CV, small enough that a mis-drop cannot exhaust memory.
MAX_UPLOAD_BYTES = 15 * 1024 * 1024


@router.post("", status_code=201)
async def upload_document(
    file: UploadFile = File(...),
    reimport: bool = False,
    settings: Settings = Depends(get_app_settings),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Upload a CV and propose facts from it.

    `reimport=true` re-extracts a file already uploaded before. Useful after a parser
    improvement: the new proposals arrive as *conflicts* against whatever you accepted from
    the earlier extraction, so you see exactly what would change and nothing is rewritten
    behind your back.
    """
    profile = await current_profile(session)
    if profile is None:
        raise HTTPException(400, "Create a profile before uploading a CV.")

    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            413, f"That file is {len(data) // 1_048_576} MB; the limit is "
                 f"{MAX_UPLOAD_BYTES // 1_048_576} MB."
        )

    digest = sha256(data)

    await _refuse_known_generated(session, digest)
    existing = (
        await session.execute(
            select(m.Document).where(
                m.Document.profile_id == profile.id, m.Document.sha256 == digest
            )
        )
    ).scalars().first()
    if existing is not None and not reimport:
        # Same bytes as before: return the earlier import rather than duplicating proposals.
        return await _document_payload(session, existing, already_imported=True)

    if existing is not None and reimport:
        # Drop the previous proposals from this file -- they came from the old extraction --
        # then re-extract. Accepted facts are untouched; improved values come back as
        # conflicts for you to approve.
        old = (
            await session.execute(
                select(m.ProfileFact).where(
                    m.ProfileFact.document_id == existing.id,
                    m.ProfileFact.status == FactStatus.PROPOSED.value,
                )
            )
        ).scalars().all()
        for fact in old:
            await session.delete(fact)
        await session.commit()

        try:
            extracted = extract(data, file.filename or "")
        except ExtractionError as exc:
            raise HTTPException(422, str(exc)) from exc

        _refuse_generated_text(extracted.text, file.filename or "")

        existing.text = extracted.text
        existing.text_chars = extracted.chars
        existing.parser = extracted.parser
        session.add(existing)
        await session.commit()

        await _import_facts(session, profile.id, existing, extracted.text)
        return await _document_payload(session, existing)

    try:
        extracted = extract(data, file.filename or "")
    except ExtractionError as exc:
        # Record the failure so the user can see what was tried and why it did not work.
        failed = m.Document(
            profile_id=profile.id,
            filename=file.filename or "upload",
            content_type=file.content_type or "",
            size_bytes=len(data),
            sha256=digest,
            error=str(exc),
        )
        session.add(failed)
        await session.commit()
        raise HTTPException(422, str(exc)) from exc

    _refuse_generated_text(extracted.text, file.filename or "")

    settings.ensure_dirs()
    store = settings.data_dir / "documents"
    store.mkdir(parents=True, exist_ok=True)
    name = file.filename or ""
    suffix = ("." + name.rsplit(".", 1)[-1]) if "." in name else ""
    stored_path = store / f"{digest[:16]}{suffix}"
    stored_path.write_bytes(data)

    document = m.Document(
        profile_id=profile.id,
        filename=file.filename or "upload",
        content_type=file.content_type or "",
        size_bytes=len(data),
        sha256=digest,
        stored_path=str(stored_path),
        parser=extracted.parser,
        text=extracted.text,
        text_chars=extracted.chars,
        page_count=extracted.page_count,
    )
    session.add(document)
    await session.commit()

    await _import_facts(session, profile.id, document, extracted.text)
    return await _document_payload(session, document)


async def _import_facts(
    session: AsyncSession, profile_id: UUID, document: m.Document, text: str
) -> None:
    """Extract, reconcile, and write proposals. Writes nothing usable."""
    extraction = CVParser().parse(text)

    current = list(
        (
            await session.execute(
                select(m.ProfileFact).where(m.ProfileFact.profile_id == profile_id)
            )
        ).scalars().all()
    )

    for proposal in reconcile(extraction.facts, current):
        if not proposal.actionable:
            continue
        session.add(
            m.ProfileFact(
                profile_id=profile_id,
                key=proposal.fact.key,
                value=proposal.fact.value,
                category=proposal.fact.category,
                source=FactSource.CV_IMPORT.value,
                confidence=proposal.fact.confidence,
                # Every imported fact starts here. Nothing is usable until you accept it.
                status=FactStatus.PROPOSED.value,
                document_id=document.id,
                supersedes_id=proposal.supersedes_id,
                evidence=proposal.fact.evidence,
                detail=proposal.fact.detail,
            )
        )
    await session.commit()


async def _document_payload(
    session: AsyncSession, document: m.Document, already_imported: bool = False
) -> dict:
    proposals = list(
        (
            await session.execute(
                select(m.ProfileFact).where(
                    m.ProfileFact.document_id == document.id,
                    m.ProfileFact.status == FactStatus.PROPOSED.value,
                )
            )
        ).scalars().all()
    )
    by_category: dict[str, int] = {}
    for fact in proposals:
        by_category[fact.category] = by_category.get(fact.category, 0) + 1

    return {
        "document": {
            "id": str(document.id),
            "filename": document.filename,
            "parser": document.parser,
            "size_bytes": document.size_bytes,
            "text_chars": document.text_chars,
            "page_count": document.page_count,
            "uploaded_at": document.uploaded_at.isoformat(),
        },
        "already_imported": already_imported,
        "pending_facts": len(proposals),
        "by_category": by_category,
    }


#: Legacy marker. Documents generated before the footer was removed carry this line, and
#: they are still sitting in people's Downloads folders.
_GENERATED_MARKER = "Generated by LocalApply"


async def _refuse_known_generated(session: AsyncSession, digest: str) -> None:
    """Reject a CV that LocalApply itself produced.

    Uploading a generated CV back in creates a feedback loop: the parser treats generated
    prose as source facts, so each round is built from the round before and the quality
    collapses. One real case produced an "experience" entry whose bullet was the generator's
    own footer -- "Generated by LocalApply ... from 61 verified profile facts" -- which then
    appeared on the next CV.

    Two checks: the exact PDF we wrote, and the legacy footer text for older files.
    """
    known = (
        await session.execute(
            select(m.GeneratedDocument).where(m.GeneratedDocument.pdf_sha256 == digest)
        )
    ).scalars().first()

    if known is not None:
        raise HTTPException(
            409,
            f"That file is a CV LocalApply generated ({known.kind.replace('_', ' ')} "
            f"v{known.version}). Importing it would build your profile from its own output. "
            "Upload the CV you wrote instead.",
        )

def _refuse_generated_text(text: str, filename: str) -> None:
    """The legacy check, run on extracted text.

    PDF content streams are compressed, so the marker is not visible in the raw bytes --
    an earlier version searched those and would never have matched.
    """
    if _GENERATED_MARKER in text:
        raise HTTPException(
            409,
            f"{filename or 'That file'} looks like a CV LocalApply generated -- it carries "
            "the generator's footer. Upload the CV you wrote instead; importing a generated "
            "one builds your profile out of its own output.",
        )


@router.get("")
async def list_documents(session: AsyncSession = Depends(get_session)) -> list[dict]:
    result = await session.execute(select(m.Document).order_by(m.Document.uploaded_at.desc()))
    return [
        {
            "id": str(d.id),
            "filename": d.filename,
            "parser": d.parser,
            "text_chars": d.text_chars,
            "error": d.error,
            "uploaded_at": d.uploaded_at.isoformat(),
        }
        for d in result.scalars().all()
    ]


@router.get("/{document_id}/facts")
async def document_facts(
    document_id: UUID,
    status: str = FactStatus.PROPOSED.value,
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    """Proposals from one upload, with what each would replace."""
    result = await session.execute(
        select(m.ProfileFact)
        .where(m.ProfileFact.document_id == document_id, m.ProfileFact.status == status)
        .order_by(m.ProfileFact.category, m.ProfileFact.key)
    )
    facts = list(result.scalars().all())

    payload = []
    for fact in facts:
        replaces = None
        if fact.supersedes_id is not None:
            previous = await session.get(m.ProfileFact, fact.supersedes_id)
            replaces = previous.value if previous else None
        payload.append(
            {
                **fact.model_dump(mode="json"),
                "verdict": Verdict.CONFLICT.value if replaces else Verdict.NEW.value,
                "replaces": replaces,
            }
        )
    return payload


@router.get("/{document_id}/text")
async def document_text(
    document_id: UUID, session: AsyncSession = Depends(get_session)
) -> dict:
    """The extracted text, so a proposal can be checked against its source."""
    document = await session.get(m.Document, document_id)
    if document is None:
        raise HTTPException(404, "No such document.")
    return {"id": str(document.id), "filename": document.filename, "text": document.text}


@router.post("/{document_id}/accept-all")
async def accept_all(
    document_id: UUID,
    category: str | None = None,
    min_confidence: float = 0.0,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Bulk-accept proposals from one upload.

    Conflicts are deliberately excluded: replacing a fact you already confirmed is exactly
    the decision that should not be made in bulk. Filter by category and confidence to keep
    a bulk accept narrow.
    """
    query = select(m.ProfileFact).where(
        m.ProfileFact.document_id == document_id,
        m.ProfileFact.status == FactStatus.PROPOSED.value,
        m.ProfileFact.supersedes_id.is_(None),
        m.ProfileFact.confidence >= min_confidence,
    )
    if category:
        query = query.where(m.ProfileFact.category == category)

    facts = list((await session.execute(query)).scalars().all())
    for fact in facts:
        fact.status = FactStatus.ACCEPTED.value
        fact.resolved_at = utc_now()
        session.add(fact)
    await session.commit()

    skipped = list(
        (
            await session.execute(
                select(m.ProfileFact).where(
                    m.ProfileFact.document_id == document_id,
                    m.ProfileFact.status == FactStatus.PROPOSED.value,
                )
            )
        ).scalars().all()
    )
    return {
        "accepted": len(facts),
        "still_pending": len(skipped),
        "note": "Conflicts are never bulk-accepted; review those individually.",
    }


@router.get("/{document_id}/summary")
async def document_summary(
    document_id: UUID, session: AsyncSession = Depends(get_session)
) -> dict:
    """Re-run extraction against the stored text without writing anything.

    Useful for seeing what the parser makes of a document -- including what it warns it
    could not find -- before or after deciding on the proposals.
    """
    document = await session.get(m.Document, document_id)
    if document is None:
        raise HTTPException(404, "No such document.")

    extraction = CVParser().parse(document.text)
    current = list(
        (
            await session.execute(
                select(m.ProfileFact).where(m.ProfileFact.profile_id == document.profile_id)
            )
        ).scalars().all()
    )
    proposals = reconcile(extraction.facts, current)
    return {
        "parser": CVParser.name,
        "sections": sorted(extraction.sections.keys()),
        "extracted": len(extraction.facts),
        "counts": summarise(proposals),
        "warnings": extraction.warnings,
    }
