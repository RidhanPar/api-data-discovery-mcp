"""Shared fixtures.

Integration tests need Postgres with pgvector. By default a throwaway container is
started with testcontainers; set NORDLYS_TEST_DATABASE_URL to use an existing
database instead (e.g. in CI with a service container). If neither is possible the
integration tests are skipped, not failed - unit tests still run everywhere.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from nordlys_discovery.catalog.loader import DEFAULT_CATALOG_DIR
from nordlys_discovery.db.session import make_engine
from nordlys_discovery.embeddings.others import HashingEmbeddings

ROOT = Path(__file__).resolve().parents[1]
PGVECTOR_IMAGE = "pgvector/pgvector:pg16"


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    url = os.environ.get("NORDLYS_TEST_DATABASE_URL")
    if url:
        yield url
        return
    try:
        try:
            from testcontainers.community.postgres import PostgresContainer  # type: ignore[import-not-found]
        except ImportError:
            from testcontainers.postgres import PostgresContainer

        container = PostgresContainer(PGVECTOR_IMAGE, driver="psycopg")
        container.start()
    except Exception as exc:  # Docker not available
        pytest.skip(f"no database for integration tests: {exc}")
    try:
        yield container.get_connection_url()
    finally:
        container.stop()


@pytest.fixture(scope="session")
def engine(database_url: str) -> Iterator[Engine]:
    # Build the schema through the real migration, so the migration itself is tested.
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.attributes["database_url"] = database_url
    command.upgrade(cfg, "head")
    eng = make_engine(database_url, pool_size=2, statement_timeout_ms=10_000)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """A session on an empty schema; tables are truncated after each test."""
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield s
    finally:
        s.rollback()
        s.execute(text("TRUNCATE chunk, api_spec, data_product RESTART IDENTITY CASCADE"))
        s.commit()
        s.close()


@pytest.fixture
def hashing_embedder() -> HashingEmbeddings:
    return HashingEmbeddings(384)


@pytest.fixture
def catalog_copy(tmp_path: Path) -> Path:
    """A writable copy of the catalog for tests that modify files."""
    dest = tmp_path / "catalog"
    shutil.copytree(DEFAULT_CATALOG_DIR, dest)
    return dest
