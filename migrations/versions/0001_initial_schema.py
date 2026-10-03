"""Initial schema: api_spec, data_product, chunk (with tsvector + HNSW vector index).

Revision ID: 0001
Revises:
Create Date: 2026-10-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "api_spec",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("api_id", sa.String(120), nullable=False, index=True),
        sa.Column("major_version", sa.Integer, nullable=False),
        sa.Column("version", sa.String(40), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("domain", sa.String(40), nullable=False, index=True),
        sa.Column("owner_team", sa.String(120), nullable=False),
        sa.Column("countries", postgresql.ARRAY(sa.String(2)), nullable=False),
        sa.Column("audience", sa.String(40), nullable=False),
        sa.Column("data_classification", sa.String(20)),
        sa.Column("lifecycle_status", sa.String(20), nullable=False),
        sa.Column("sunset", sa.Date),
        sa.Column("replacement", sa.String(160)),
        sa.Column("metadata_provenance", postgresql.JSONB, nullable=False),
        sa.Column("source_path", sa.String(300), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("raw", postgresql.JSONB, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("api_id", "major_version", name="uq_api_spec_version"),
    )

    op.create_table(
        "data_product",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("product_id", sa.String(120), nullable=False, unique=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("version", sa.String(40), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("domain", sa.String(40), nullable=False, index=True),
        sa.Column("owner_team", sa.String(120), nullable=False),
        sa.Column("countries", postgresql.ARRAY(sa.String(2)), nullable=False),
        sa.Column("pii_classification", sa.String(20), nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("source_path", sa.String(300), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("raw", postgresql.JSONB, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "chunk",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("chunk_key", sa.String(400), nullable=False, unique=True),
        sa.Column("kind", sa.String(30), nullable=False, index=True),
        sa.Column("api_spec_id", sa.Integer, sa.ForeignKey("api_spec.id", ondelete="CASCADE"), index=True),
        sa.Column("data_product_id", sa.Integer, sa.ForeignKey("data_product.id", ondelete="CASCADE"), index=True),
        sa.Column("asset_type", sa.String(20), nullable=False),
        sa.Column("asset_id", sa.String(120), nullable=False),
        sa.Column("major_version", sa.Integer),
        sa.Column("domain", sa.String(40), nullable=False, index=True),
        sa.Column("countries", postgresql.ARRAY(sa.String(2)), nullable=False),
        sa.Column("deprecated", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("pii_level", sa.String(20)),
        sa.Column("method", sa.String(10)),
        sa.Column("path", sa.String(300)),
        sa.Column("field_name", sa.String(120)),
        sa.Column("title", sa.String(400), nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("embedding", Vector(384)),
        sa.Column("embedding_model", sa.String(120)),
        sa.Column(
            "tsv",
            postgresql.TSVECTOR,
            sa.Computed("to_tsvector('english', coalesce(title, '') || ' ' || coalesce(body, ''))", persisted=True),
        ),
        sa.CheckConstraint("(api_spec_id IS NULL) <> (data_product_id IS NULL)", name="ck_chunk_one_parent"),
    )
    op.create_index("ix_chunk_tsv", "chunk", ["tsv"], postgresql_using="gin")
    op.create_index("ix_chunk_countries", "chunk", ["countries"], postgresql_using="gin")
    op.create_index(
        "ix_chunk_embedding_hnsw",
        "chunk",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )


def downgrade() -> None:
    op.drop_table("chunk")
    op.drop_table("data_product")
    op.drop_table("api_spec")
