"""spend_exceptions: requests for more budget, and what was decided (ADR 0032)

Revision ID: 0011
Revises: 0010
"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"


def upgrade() -> None:
    op.create_table(
        "spend_exceptions",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("subject", sa.String(100), nullable=False),
        sa.Column("subject_name", sa.String(100), nullable=False),
        sa.Column("spend_id", sa.String(100), nullable=False),
        sa.Column("period", sa.String(7), nullable=False),
        sa.Column("amount_usd", sa.Float(), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("status", sa.String(8), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("granted_usd", sa.Float()),
        sa.Column("decided_by", sa.String(100)),
        sa.Column("decided_by_name", sa.String(100)),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("note", sa.String(500)),
    )
    op.create_index("ix_spend_exceptions_subject", "spend_exceptions", ["subject"])
    op.create_index("ix_spend_exceptions_requested_at", "spend_exceptions", ["requested_at"])
    op.create_index(
        "ix_spend_exceptions_grant", "spend_exceptions", ["spend_id", "period", "status"]
    )
    # One open request per caller and month.
    op.create_index(
        "uq_spend_exceptions_pending",
        "spend_exceptions",
        ["spend_id", "period"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_table("spend_exceptions")
