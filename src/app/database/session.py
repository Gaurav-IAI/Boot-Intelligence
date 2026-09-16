"""Engine / session handling.

PostgreSQL is the target. If it is unreachable and `ALLOW_SQLITE_FALLBACK` is on,
the POC degrades to SQLite so the pipeline still runs — but `active_backend()`
reports which one was used, and the CLI and dashboard both show it, so a run is
never silently non-Postgres.
"""
from __future__ import annotations

import logging

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from ..config import settings
from .migrations import apply_additive_migrations
from .models import Base

log = logging.getLogger(__name__)

_engine: Engine | None = None
_backend: str = "unknown"
_SessionFactory: sessionmaker[Session] | None = None


def _try_engine(url: str) -> Engine | None:
    try:
        eng = create_engine(url, pool_pre_ping=True, future=True)
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        return eng
    except Exception as exc:
        log.warning("cannot use database %r: %s", url.split("@")[-1], exc)
        return None


def get_engine() -> Engine:
    global _engine, _backend, _SessionFactory
    if _engine is not None:
        return _engine

    eng = _try_engine(settings.database_url)
    if eng is not None:
        _backend = "postgresql"
    elif settings.allow_sqlite_fallback:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        eng = _try_engine(settings.database_url_fallback)
        if eng is None:
            raise RuntimeError("neither PostgreSQL nor the SQLite fallback is reachable")
        _backend = "sqlite (FALLBACK — PostgreSQL was unreachable)"
        log.warning("USING SQLITE FALLBACK. Start PostgreSQL with: docker compose up -d")
    else:
        raise RuntimeError(
            f"PostgreSQL at {settings.database_url.split('@')[-1]} is unreachable and "
            "ALLOW_SQLITE_FALLBACK is false. Run: docker compose up -d"
        )

    # Models may declare columns added after a database was first created; add
    # them before any ORM query selects them.
    apply_additive_migrations(eng)

    _engine = eng
    _SessionFactory = sessionmaker(bind=eng, expire_on_commit=False, future=True)
    return _engine


def active_backend() -> str:
    get_engine()
    return _backend


def init_db(drop: bool = False) -> str:
    eng = get_engine()
    if drop:
        Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)
    apply_additive_migrations(eng)
    return active_backend()


def get_session() -> Session:
    get_engine()
    assert _SessionFactory is not None
    return _SessionFactory()
