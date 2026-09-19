"""SQLAlchemy engine/session setup.

The gapi database lives entirely under ``gapi/data/`` and never touches
``server/data/``. SQLite is opened in WAL mode with a busy timeout so the
atomic billing transactions (BEGIN IMMEDIATE, Phase 3) wait for a lock
instead of failing immediately under concurrency.
"""

from __future__ import annotations

import os
from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


def _ensure_sqlite_dir(url: str) -> None:
    """Create the parent directory for a sqlite URL like sqlite:///./data/x.db."""
    if not url.startswith("sqlite"):
        return
    # sqlite:///./data/gapi.db → ./data ; sqlite:////abs/path.db handled too.
    tail = url.split("///", 1)[1]
    db_path = tail.lstrip("/") if not tail.startswith("/") else tail
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)


_ensure_sqlite_dir(settings.database_url)

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_conn, _record):
    # Bound to *this* engine, not the Engine class: a class-level listener
    # would also fire for any other engine in the process (Alembic's, a test
    # harness's, a future Postgres one) and issue PRAGMAs it cannot parse.
    if not settings.database_url.startswith("sqlite"):
        return
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency: one session per request, rolled back on error."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
