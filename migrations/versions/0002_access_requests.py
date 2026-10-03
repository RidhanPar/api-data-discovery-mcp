"""Access requests: human-approved, never auto-granted.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "access_request",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("requester", sa.String(200), nullable=False, index=True),
        sa.Column("asset_type", sa.String(20), nullable=False),
        sa.Column("asset_id", sa.String(120), nullable=False),
        sa.Column("major_version", sa.Integer),
        sa.Column("purpose", sa.String(120), nullable=False),
        sa.Column("justification", sa.Text, nullable=False),
        sa.Column("requested_scope", sa.String(200), nullable=False),
        sa.Column("approval_route", sa.String(60), nullable=False),
        sa.Column("approvers", postgresql.ARRAY(sa.String(120)), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("decided_by", sa.String(200)),
        sa.Column("decision_note", sa.Text),
        sa.CheckConstraint(
            "status IN ('pending_approval', 'approved', 'rejected', 'withdrawn')", name="ck_access_request_status"
        ),
        # A decision always records who made it, and never the requester themself.
        sa.CheckConstraint("(status = 'pending_approval') = (decided_by IS NULL)", name="ck_access_request_decided_by"),
        sa.CheckConstraint("decided_by IS NULL OR decided_by <> requester", name="ck_access_request_four_eyes"),
    )
    # At most one open request per requester/asset/purpose: makes request_access idempotent.
    op.create_index(
        "uq_access_request_open",
        "access_request",
        ["requester", "asset_type", "asset_id", sa.text("coalesce(major_version, 0)"), "purpose"],
        unique=True,
        postgresql_where=sa.text("status = 'pending_approval'"),
    )


def downgrade() -> None:
    op.drop_index("uq_access_request_open", table_name="access_request")
    op.drop_table("access_request")
