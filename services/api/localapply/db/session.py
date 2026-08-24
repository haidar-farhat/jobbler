"""Async engine and session factory."""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlmodel import SQLModel

from ..config import Settings, get_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _sqlite_tuning(engine: AsyncEngine) -> None:
    """Make SQLite survive the concurrent writes a live run produces.

    A run persists an event, an action, and a state change from separate short-lived
    sessions while the dashboard reads alongside it. SQLite's default rollback journal
    allows one writer and gives up after five seconds, which surfaced as intermittent
    "database is locked" failures once the event log started being written.

    WAL lets readers run concurrently with a writer, and the busy timeout makes a contended
    write wait instead of failing. Postgres needs neither, so this is scoped to SQLite.
    """

    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragmas(dbapi_connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()


def init_engine(settings: Settings | None = None) -> AsyncEngine:
    global _engine, _session_factory
    settings = settings or get_settings()
    if _engine is None:
        is_sqlite = settings.database_url.startswith("sqlite")
        _engine = create_async_engine(
            settings.database_url,
            echo=False,
            pool_pre_ping=True,
            connect_args={"timeout": 30} if is_sqlite else {},
        )
        if is_sqlite:
            _sqlite_tuning(_engine)
        _session_factory = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)
    return _engine


def get_engine() -> AsyncEngine:
    return _engine if _engine is not None else init_engine()


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


async def create_all() -> None:
    """Create tables directly from the models.

    Used by the test suite and by `scripts/dev_bootstrap.py`. Production schema changes go
    through Alembic (`alembic upgrade head`) so migrations stay the single source of truth.
    """
    engine = get_engine()
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency."""
    if _session_factory is None:
        init_engine()
    assert _session_factory is not None
    async with _session_factory() as session:
        yield session


def session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        init_engine()
    assert _session_factory is not None
    return _session_factory
