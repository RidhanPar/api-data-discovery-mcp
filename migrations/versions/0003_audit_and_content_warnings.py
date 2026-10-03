"""Append-only audit log; injection-screening results on catalog assets.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

DECISIONS = "('allowed', 'denied', 'invalid', 'error', 'pending', 'approved', 'rejected', 'expired', 'info')"


def upgrade() -> None:
    op.create_table(
        "audit_event",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("source", sa.String(60), nullable=False),  # authenticated service that wrote it
        sa.Column("actor", sa.String(200), nullable=False),  # end user (or service account)
        sa.Column("action", sa.String(80), nullable=False),  # tool name or workflow event
        sa.Column("resource", sa.String(300)),
        sa.Column("decision", sa.String(20), nullable=False),
        sa.Column("reason", sa.Text),
        sa.Column("request_id", sa.String(80)),
        sa.Column("details", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.CheckConstraint(f"decision IN {DECISIONS}", name="ck_audit_event_decision"),
    )
    op.create_index("ix_audit_event_actor_time", "audit_event", ["actor", "occurred_at"])
    op.create_index("ix_audit_event_action_time", "audit_event", ["action", "occurred_at"])

    # Append-only: the database refuses UPDATE, DELETE and TRUNCATE for every role that
    # does not own the table's triggers. In production the application role is also
    # granted INSERT/SELECT only, and events are streamed to Log Analytics.
    op.execute("""
        CREATE FUNCTION audit_event_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'audit_event is append-only (% refused)', TG_OP
                USING ERRCODE = 'insufficient_privilege';
        END $$;
    """)
    op.execute("""
        CREATE TRIGGER audit_event_no_update_delete BEFORE UPDATE OR DELETE ON audit_event
        FOR EACH ROW EXECUTE FUNCTION audit_event_immutable();
    """)
    op.execute("""
        CREATE TRIGGER audit_event_no_truncate BEFORE TRUNCATE ON audit_event
        FOR EACH STATEMENT EXECUTE FUNCTION audit_event_immutable();
    """)

    for table in ("api_spec", "data_product"):
        op.add_column(
            table,
            sa.Column(
                "content_warnings", postgresql.ARRAY(sa.String(40)), nullable=False, server_default=sa.text("'{}'")
            ),
        )


def downgrade() -> None:
    for table in ("api_spec", "data_product"):
        op.drop_column(table, "content_warnings")
    op.execute("DROP TRIGGER audit_event_no_truncate ON audit_event")
    op.execute("DROP TRIGGER audit_event_no_update_delete ON audit_event")
    op.execute("DROP FUNCTION audit_event_immutable()")
    op.drop_table("audit_event")
