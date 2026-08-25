"""Getting your data out, and back in.

Everything you have -- approved facts you corrected by hand, generated documents you actually
sent, months of job history, the audit trail behind every one of them -- lives in one Docker
volume. `POST /profile/reset` can destroy it in one request. Nothing could save it.

The archive is a plain zip of JSON plus the PDFs, and that shape is the point:

  * **It outlives this app.** A zip of readable JSON can be opened in ten years by someone
    who has never heard of LocalApply. A `pg_dump` cannot, without the right Postgres.
  * **It is the same file both ways.** Export writes exactly what import reads, and a test
    round-trips it. A backup nobody has restored is not a backup.
  * **It includes the PDFs.** A manifest listing `C:\\...\\var\\generated\\4f2a.pdf` is a
    list of files you no longer have. The bytes travel.

What it is not: a Postgres backup. Run histories, browser actions and screenshots are
deliberately left out -- they are large, they are only meaningful next to the machine that
produced them, and none of them is work you would have to redo.
"""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from .db import models as m

#: Bumped when the shape changes in a way an older import could not read. An import refuses
#: a version it does not know rather than guessing and half-restoring.
FORMAT_VERSION = 1

MANIFEST = "manifest.json"
DOCUMENTS_DIR = "documents"

#: What travels. Order matters on import: a fact references a document, an application
#: references a job, so the referenced thing has to exist first.
TABLES: tuple[tuple[str, type], ...] = (
    ("profiles", m.Profile),
    ("documents", m.Document),
    ("profile_facts", m.ProfileFact),
    ("jobs", m.Job),
    ("applications", m.Application),
    ("application_outcomes", m.ApplicationOutcome),
    ("generated_documents", m.GeneratedDocument),
    ("saved_searches", m.SavedSearch),
    ("audit_logs", m.AuditLog),
)


@dataclass
class Report:
    """What moved, per table. Counted rather than assumed."""

    rows: dict[str, int] = field(default_factory=dict)
    files: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "rows": self.rows,
            "files": self.files,
            "skipped": self.skipped,
            "notes": self.notes,
            "total": sum(self.rows.values()),
        }


def _encode(value):
    """JSON that survives a round trip.

    UUIDs and datetimes are the two types SQLModel hands back that `json` will not take, and
    both have to come back as themselves rather than as strings -- so the encoding is exact
    and reversible rather than merely readable.
    """
    if isinstance(value, UUID):
        return {"__uuid__": str(value)}
    if isinstance(value, datetime):
        return {"__datetime__": value.isoformat()}
    raise TypeError(f"cannot encode {type(value).__name__}")


def _decode(obj: dict):
    if "__uuid__" in obj:
        return UUID(obj["__uuid__"])
    if "__datetime__" in obj:
        parsed = datetime.fromisoformat(obj["__datetime__"])
        # SQLite hands back naive datetimes from timezone-aware columns, so an export taken
        # there and imported to Postgres would otherwise carry naive values into a column
        # that expects offsets.
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return obj


async def export_archive(session: AsyncSession) -> tuple[bytes, Report]:
    """Everything that is yours, as a zip.

    Held in memory rather than streamed to a temp file: the largest part by far is the PDFs,
    a few hundred kilobytes each, and a person with a thousand generated documents has other
    problems.
    """
    report = Report()
    buffer = io.BytesIO()

    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        data: dict[str, list[dict]] = {}
        for name, model in TABLES:
            rows = list((await session.execute(select(model))).scalars().all())
            data[name] = [row.model_dump() for row in rows]
            report.rows[name] = len(rows)

        for name, rows in data.items():
            archive.writestr(
                f"{name}.json",
                json.dumps(rows, indent=2, default=_encode, ensure_ascii=False),
            )

        # The PDFs themselves. A path to a file on a machine you no longer have is not a
        # backup of that file.
        for row in data.get("generated_documents", []):
            path_text = row.get("pdf_path")
            if not path_text:
                continue
            path = Path(path_text)
            if not path.is_file():
                report.skipped["missing_pdf"] = report.skipped.get("missing_pdf", 0) + 1
                continue
            archive.write(path, f"{DOCUMENTS_DIR}/{row['id']['__uuid__']}.pdf")
            report.files += 1

        archive.writestr(
            MANIFEST,
            json.dumps(
                {
                    "format_version": FORMAT_VERSION,
                    "exported_at": datetime.now(UTC).isoformat(),
                    "application": "LocalApply",
                    "rows": report.rows,
                    "files": report.files,
                    "contains": [f"{name}.json" for name, _ in TABLES],
                    "note": (
                        "Plain JSON and PDFs. Readable without LocalApply. Run histories, "
                        "browser actions and screenshots are deliberately not included: "
                        "they are large, only meaningful on the machine that made them, "
                        "and none of it is work you would have to redo."
                    ),
                },
                indent=2,
            ),
        )

    if report.skipped.get("missing_pdf"):
        report.notes.append(
            f"{report.skipped['missing_pdf']} generated document(s) had no PDF on disk. "
            "Their HTML is included and they can be re-rendered."
        )
    return buffer.getvalue(), report


class BadArchive(ValueError):
    """Not something this can import, and it says why rather than half-restoring."""


async def import_archive(
    session: AsyncSession, blob: bytes, *, settings, replace: bool = False
) -> Report:
    """Read an archive back in.

    Refuses by default onto a database that already holds a profile. Merging two profiles is
    not a thing this app has a concept of, and the failure mode -- two half-merged identities
    with facts pointing at the wrong one -- is worse than being told no.
    """
    report = Report()

    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as exc:
        raise BadArchive("That file is not a LocalApply archive.") from exc

    try:
        manifest = json.loads(archive.read(MANIFEST))
    except KeyError as exc:
        raise BadArchive("That archive has no manifest, so it is not one of ours.") from exc
    except json.JSONDecodeError as exc:
        raise BadArchive("That archive's manifest is unreadable.") from exc

    version = manifest.get("format_version")
    if version != FORMAT_VERSION:
        raise BadArchive(
            f"That archive is format version {version}; this build reads version "
            f"{FORMAT_VERSION}. Importing it could restore half of it."
        )

    existing = (await session.execute(select(m.Profile))).scalars().first()
    if existing is not None and not replace:
        raise BadArchive(
            "There is already a profile here. Importing would merge two identities, which "
            "this app has no concept of. Send replace=true to overwrite what is here."
        )

    if existing is not None:
        await _clear(session)
        report.notes.append("The existing profile was replaced.")

    # Insert in declaration order, so a row's references already exist.
    for name, model in TABLES:
        try:
            rows = json.loads(archive.read(f"{name}.json"), object_hook=_decode)
        except KeyError:
            # An archive from a build without this table. Not fatal -- the rest still
            # restores, and saying which part was absent beats refusing the whole thing.
            report.skipped[name] = 0
            report.notes.append(f"That archive had no {name}; the rest was restored.")
            continue

        for row in rows:
            session.add(model(**row))
        report.rows[name] = len(rows)
        await session.commit()

    # The PDFs, back where the app expects to find them.
    settings.ensure_dirs()
    target = settings.data_dir / "generated"
    target.mkdir(parents=True, exist_ok=True)

    for entry in archive.namelist():
        if not entry.startswith(f"{DOCUMENTS_DIR}/") or not entry.endswith(".pdf"):
            continue
        document_id = Path(entry).stem
        destination = target / f"{document_id}.pdf"
        destination.write_bytes(archive.read(entry))
        report.files += 1

        # The stored path pointed at the machine this came from. Repoint it at this one, or
        # every download 410s with the file sitting right there.
        document = await session.get(m.GeneratedDocument, UUID(document_id))
        if document is not None:
            document.pdf_path = str(destination)
            session.add(document)
    await session.commit()

    return report


async def _clear(session: AsyncSession) -> None:
    """Delete what is here, in reverse dependency order.

    `supersedes_id` points from one fact to another, so those links are cut before the rows
    go -- the same reason `POST /profile/reset` does it.
    """
    facts = list((await session.execute(select(m.ProfileFact))).scalars().all())
    for fact in facts:
        fact.supersedes_id = None
        session.add(fact)
    await session.flush()

    for _name, model in reversed(TABLES):
        for row in (await session.execute(select(model))).scalars().all():
            await session.delete(row)
    await session.commit()
