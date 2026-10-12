"""directory: users, groups and memberships provisioned over SCIM (ADR 0029)

Revision ID: 0010
Revises: 0009
"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"


def upgrade() -> None:
    op.create_table(
        "directory_users",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_name", sa.String(320), nullable=False),
        sa.Column("external_id", sa.String(255)),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("display_name", sa.String(255)),
        sa.Column("email", sa.String(320)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_directory_users_user_name", "directory_users", ["user_name"], unique=True)
    op.create_index("ix_directory_users_external_id", "directory_users", ["external_id"])
    op.create_table(
        "directory_groups",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("external_id", sa.String(255)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_directory_groups_display_name", "directory_groups", ["display_name"], unique=True
    )
    op.create_table(
        "directory_members",
        sa.Column(
            "group_id",
            sa.Uuid(),
            sa.ForeignKey("directory_groups.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("directory_users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )
    op.create_index("ix_directory_members_user_id", "directory_members", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_directory_members_user_id", table_name="directory_members")
    op.drop_table("directory_members")
    op.drop_index("ix_directory_groups_display_name", table_name="directory_groups")
    op.drop_table("directory_groups")
    op.drop_index("ix_directory_users_external_id", table_name="directory_users")
    op.drop_index("ix_directory_users_user_name", table_name="directory_users")
    op.drop_table("directory_users")
