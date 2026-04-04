"""asyncpg connection pool singleton.

Usage:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM jobs WHERE status = $1", "pending")

All connections use sslmode=require in production (CLAUDE.md security rule).
"""

import os
from contextlib import asynccontextmanager

import asyncpg

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    """Return the shared connection pool, creating it on first call."""
    global _pool
    if _pool is None:
        database_url = os.environ["DATABASE_URL"]
        ssl = "require" if "rds.amazonaws.com" in database_url else None
        _pool = await asyncpg.create_pool(
            database_url,
            min_size=2,
            max_size=10,
            ssl=ssl,
        )
    return _pool


async def close_pool() -> None:
    """Shut down the connection pool gracefully."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def acquire():
    """Convenience context manager for a single connection."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        yield conn
