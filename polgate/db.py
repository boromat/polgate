"""Database engine and session dependency (SQLite via SQLModel)."""

from collections.abc import Generator
from typing import Any

from sqlalchemy import Engine, event
from sqlmodel import Session, SQLModel, create_engine

from polgate.config import get_settings

_engine: Engine | None = None

#: How long a writer waits for another writer before giving up, in milliseconds. SQLite's default is
#: zero, which turns a moment of overlap between two employees into "database is locked".
BUSY_TIMEOUT_MS = 5000


def _apply_sqlite_pragmas(connection: Any, _record: Any) -> None:
    """Configure every new SQLite connection.

    ``WAL`` lets readers carry on while somebody writes, which for this hub means one person sending
    a message does not block everybody else's page from loading. ``foreign_keys`` is off by default
    in SQLite, so without this line the references between conversations, messages and vault rows
    are documentation rather than constraints.
    """
    cursor = connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def make_engine(database_url: str) -> Engine:
    """Build an engine configured the way this application needs it.

    Tests build their own engines through this function rather than calling ``create_engine``
    directly, so what they exercise is what runs in production, pragmas included.
    """
    is_sqlite = database_url.startswith("sqlite")
    connect_args = {"check_same_thread": False} if is_sqlite else {}
    engine = create_engine(database_url, connect_args=connect_args)
    if is_sqlite:
        event.listen(engine, "connect", _apply_sqlite_pragmas)
    return engine


def get_engine() -> Engine:
    """Return the process-wide database engine, creating it on first use."""
    global _engine
    if _engine is None:
        _engine = make_engine(get_settings().database_url)
    return _engine


def init_db() -> None:
    """Create missing tables, then bring an older database up to the current models.

    ``create_all`` handles a database that has no tables yet and ignores one that already has them,
    so a field added to a model after the first run needs the second step; see
    :mod:`polgate.schema`.
    """
    from polgate import models  # noqa: F401
    from polgate.schema import reconcile

    engine = get_engine()
    SQLModel.metadata.create_all(engine)
    reconcile(engine)


def get_session() -> Generator[Session, None, None]:
    """Yield a database session for a request; FastAPI closes it afterwards."""
    with Session(get_engine()) as session:
        yield session
