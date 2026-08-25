"""Export and import: getting your data out, and back.

One archive, both directions. Everything that is actually yours -- the facts you corrected by
hand, the documents you sent, your job history and its audit trail -- in plain JSON with the
PDFs beside it, readable by someone who has never heard of this app.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import Settings
from ...db.session import get_session
from ...portability import FORMAT_VERSION, BadArchive, export_archive, import_archive
from ..deps import get_app_settings

router = APIRouter(prefix="/backup", tags=["backup"])

#: Bigger than this is not a LocalApply archive. A generous ceiling -- a thousand generated
#: PDFs is a few hundred megabytes -- but an unbounded upload read into memory is not.
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024


@router.get("/export")
async def export_everything(session: AsyncSession = Depends(get_session)) -> Response:
    """Download everything, as a zip you could open in ten years."""
    blob, report = await export_archive(session)
    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    return Response(
        content=blob,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="localapply-{stamp}.zip"',
            # So a client can show what it got without unzipping it.
            "X-LocalApply-Rows": str(sum(report.rows.values())),
            "X-LocalApply-Files": str(report.files),
        },
    )


@router.get("/preview")
async def preview(session: AsyncSession = Depends(get_session)) -> dict:
    """What an export would contain, without downloading it.

    So "back up before you do something destructive" can show what is at stake rather than
    asking someone to take it on faith.
    """
    _, report = await export_archive(session)
    return {"format_version": FORMAT_VERSION, **report.as_dict()}


@router.post("/import")
async def import_everything(
    file: UploadFile = File(...),
    replace: bool = Query(
        False,
        description="Overwrite the profile that is already here. Without it, an import onto "
        "a populated database is refused rather than merged.",
    ),
    settings: Settings = Depends(get_app_settings),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Restore an archive.

    Refused by default onto a database that already has a profile: merging two identities is
    not a thing this app has a concept of, and two half-merged profiles with facts pointing
    at the wrong one is worse than being told no.
    """
    blob = await file.read()
    if not blob:
        raise HTTPException(400, "That file was empty.")
    if len(blob) > MAX_ARCHIVE_BYTES:
        raise HTTPException(
            413, f"That archive is larger than {MAX_ARCHIVE_BYTES // (1024 * 1024)} MB."
        )

    try:
        report = await import_archive(session, blob, settings=settings, replace=replace)
    except BadArchive as exc:
        raise HTTPException(400, str(exc)) from exc

    return {
        **report.as_dict(),
        "note": "Everything in that archive is now here. Nothing was fetched or generated.",
    }
