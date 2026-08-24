"""cv import: documents table and fact lifecycle

Replaces `profile_facts.verified` (bool) with `status` (accepted | proposed | rejected |
superseded), and adds the `documents` table plus provenance columns.

Hand-corrected after autogenerate. Alembic emitted `add_column(status, nullable=False)` with
no default and an unconditional `drop_column(verified)`, which would have failed on any
non-empty table and, worse, discarded which facts you had actually confirmed -- leaving them
unusable and silently emptying the agent's profile. The backfill below is the whole point of
this migration.

Revision ID: 7fe8f220df0f
Revises: 6a061b138e44
Create Date: 2026-08-24 15:05:30.054922
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "7fe8f220df0f"
down_revision: str | None = "6a061b138e44"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("filename", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("content_type", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("stored_path", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("parser", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("text", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("text_chars", sa.Integer(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("error", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["profile_id"], ["profiles.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_documents_profile_id"), "documents", ["profile_id"], unique=False)
    op.create_index(op.f("ix_documents_sha256"), "documents", ["sha256"], unique=False)

    op.add_column("profile_facts", sa.Column("document_id", sa.Uuid(), nullable=True))
    op.add_column("profile_facts", sa.Column("supersedes_id", sa.Uuid(), nullable=True))
    op.add_column(
        "profile_facts", sa.Column("evidence", sqlmodel.sql.sqltypes.AutoString(), nullable=True)
    )
    op.add_column(
        "profile_facts", sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True)
    )

    # Three steps, so existing rows survive with their meaning intact:
    #   1. add nullable, 2. backfill from `verified`, 3. tighten to NOT NULL.
    op.add_column(
        "profile_facts",
        sa.Column("status", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    )
    op.execute(
        "UPDATE profile_facts SET status = CASE WHEN verified THEN 'accepted' "
        "ELSE 'proposed' END"
    )
    op.alter_column("profile_facts", "status", nullable=False)

    op.create_index(
        op.f("ix_profile_facts_document_id"), "profile_facts", ["document_id"], unique=False
    )
    op.create_index(op.f("ix_profile_facts_status"), "profile_facts", ["status"], unique=False)
    op.create_foreign_key(
        "fk_profile_facts_supersedes", "profile_facts", "profile_facts",
        ["supersedes_id"], ["id"],
    )
    op.create_foreign_key(
        "fk_profile_facts_document", "profile_facts", "documents", ["document_id"], ["id"]
    )

    # Only now that the meaning is preserved elsewhere.
    op.drop_column("profile_facts", "verified")


def downgrade() -> None:
    # Mirror image: restore `verified` from `status` before dropping it, so a rollback does
    # not lose which facts were confirmed either.
    op.add_column(
        "profile_facts",
        sa.Column("verified", sa.BOOLEAN(), autoincrement=False, nullable=True),
    )
    op.execute("UPDATE profile_facts SET verified = (status = 'accepted')")
    op.alter_column("profile_facts", "verified", nullable=False)

    op.drop_constraint("fk_profile_facts_document", "profile_facts", type_="foreignkey")
    op.drop_constraint("fk_profile_facts_supersedes", "profile_facts", type_="foreignkey")
    op.drop_index(op.f("ix_profile_facts_status"), table_name="profile_facts")
    op.drop_index(op.f("ix_profile_facts_document_id"), table_name="profile_facts")

    op.drop_column("profile_facts", "status")
    op.drop_column("profile_facts", "resolved_at")
    op.drop_column("profile_facts", "evidence")
    op.drop_column("profile_facts", "supersedes_id")
    op.drop_column("profile_facts", "document_id")

    op.drop_index(op.f("ix_documents_sha256"), table_name="documents")
    op.drop_index(op.f("ix_documents_profile_id"), table_name="documents")
    op.drop_table("documents")
