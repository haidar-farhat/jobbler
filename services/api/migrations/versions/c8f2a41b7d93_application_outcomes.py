"""What happened after you applied.

The application state machine ends at SUBMITTED and nothing recorded what came next, so the
app could apply to two hundred jobs and never tell you whether any of it worked.

A separate table rather than a column on `applications`, for two reasons. `applications.state`
has exactly two writers and describes what the *agent* did; an outcome describes what the
*employer* did and is typed in by a person weeks later. And a history is what makes the
interesting questions answerable -- applied, heard back on day nine, screened, interviewed
twice, rejected -- where a single status column answers none of them.

Revision ID: c8f2a41b7d93
Revises: b3d7c1a94e20
"""

from __future__ import annotations

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "c8f2a41b7d93"
down_revision: str | None = "b3d7c1a94e20"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "application_outcomes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("note", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_application_outcomes_application_id"),
        "application_outcomes",
        ["application_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_application_outcomes_kind"), "application_outcomes", ["kind"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_application_outcomes_kind"), table_name="application_outcomes")
    op.drop_index(
        op.f("ix_application_outcomes_application_id"), table_name="application_outcomes"
    )
    op.drop_table("application_outcomes")
