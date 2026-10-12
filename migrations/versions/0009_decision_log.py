"""decision_log: one row per governance decision, allow or deny (ADR 0028)

Revision ID: 0009
Revises: 0008
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"


def upgrade() -> None:
    op.create_table(
        "decision_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),  # append-only
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("subject", sa.String(100), nullable=False),
        sa.Column("subject_kind", sa.String(16), nullable=False),
        sa.Column("subject_name", sa.String(100), nullable=False),
        sa.Column("roles", sa.JSON(), nullable=False),
        sa.Column("tier", sa.String(50), nullable=False),
        sa.Column("team", sa.String(100)),
        sa.Column("action", sa.String(200), nullable=False),
        sa.Column("data_class", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(8), nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("reason", sa.String(300), nullable=False),
        sa.Column("policy_version", sa.String(32), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False),
    )
    op.create_index("ix_decision_log_at", "decision_log", ["at"])
    op.create_index("ix_decision_log_request_id", "decision_log", ["request_id"])
    op.create_index("ix_decision_log_subject_at", "decision_log", ["subject", "at"])


def downgrade() -> None:
    op.drop_index("ix_decision_log_subject_at", table_name="decision_log")
    op.drop_index("ix_decision_log_request_id", table_name="decision_log")
    op.drop_index("ix_decision_log_at", table_name="decision_log")
    op.drop_table("decision_log")
