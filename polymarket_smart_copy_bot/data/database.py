from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from config.settings import BASE_DIR, settings

ASYNC_DATABASE_URL = settings.async_database_url
SYNC_DATABASE_URL = settings.sync_database_url


def _engine_kwargs(url: str) -> dict:
    kwargs: dict = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return kwargs


async_engine: AsyncEngine = create_async_engine(
    ASYNC_DATABASE_URL,
    **_engine_kwargs(ASYNC_DATABASE_URL),
)

AsyncSessionFactory = async_sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield a transactional async session."""

    async with AsyncSessionFactory() as session:
        yield session


async def check_database_connection() -> None:
    """Fail fast on startup when database connectivity is unavailable."""

    async with async_engine.begin() as conn:
        await conn.run_sync(lambda _: None)


def get_scheduler_jobstore_url() -> str:
    """Return a sync SQLAlchemy URL for APScheduler SQLAlchemyJobStore."""

    return SYNC_DATABASE_URL


def run_alembic_upgrade() -> None:
    """Run `alembic upgrade head` programmatically on startup."""

    alembic_ini_path = Path(BASE_DIR) / "alembic.ini"
    config = Config(str(alembic_ini_path))
    config.set_main_option("script_location", str(Path(BASE_DIR) / "alembic"))
    config.set_main_option("sqlalchemy.url", SYNC_DATABASE_URL)
    command.upgrade(config, "head")
