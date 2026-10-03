from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from ..config import get_settings


def make_engine(url: str, *, pool_size: int = 5, statement_timeout_ms: int = 5000) -> Engine:
    engine = create_engine(url, pool_size=pool_size, max_overflow=pool_size, pool_pre_ping=True, pool_recycle=1800)

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, _record):  # type: ignore[no-untyped-def]
        # Bound every query: a slow search must fail fast rather than pile up connections.
        with dbapi_conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = {int(statement_timeout_ms)}")
        dbapi_conn.commit()

    return engine


@lru_cache
def get_engine() -> Engine:
    s = get_settings()
    return make_engine(s.database_url, pool_size=s.db_pool_size, statement_timeout_ms=s.db_statement_timeout_ms)


@lru_cache
def _factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


@contextmanager
def session_scope() -> Iterator[Session]:
    session = _factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
