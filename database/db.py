import logging
import os
from pathlib import Path

import asyncpg


logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


async def connect_database() -> None:
    global _pool

    if _pool is not None:
        return

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")

    _pool = await asyncpg.create_pool(
        dsn=database_url,
        min_size=1,
        max_size=5,
        command_timeout=30,
    )

    try:
        await _run_migrations(_pool)
    except Exception:
        await _pool.close()
        _pool = None
        raise


async def close_database() -> None:
    global _pool

    if _pool is None:
        return

    await _pool.close()
    _pool = None


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool has not been initialized")
    return _pool


async def _run_migrations(pool: asyncpg.Pool) -> None:
    migrations_dir = Path(__file__).with_name("migrations")

    async with pool.acquire() as connection:
        for migration_path in sorted(migrations_dir.glob("*.sql")):
            logger.info("Applying database migration %s", migration_path.name)
            await connection.execute(migration_path.read_text(encoding="utf-8"))
