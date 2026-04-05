"""asyncpg connection pool singleton.

Usage:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM jobs WHERE status = $1", "pending")

All connections use sslmode=require in production (CLAUDE.md security rule).
"""

import asyncio
import os
from contextlib import asynccontextmanager

import asyncpg

_pool: asyncpg.Pool | None = None
_pool_loop: asyncio.AbstractEventLoop | None = None


async def get_pool() -> asyncpg.Pool:
    """Return the shared connection pool, creating it on first call.

    Automatically recreates the pool when the running event loop changes
    (e.g. between pytest-asyncio test functions).
    """
    global _pool, _pool_loop
    loop = asyncio.get_running_loop()
    if _pool is not None and _pool_loop is not loop:
        # Pool was created on a different event loop — discard it
        try:
            _pool.terminate()
        except Exception:
            pass
        _pool = None
        _pool_loop = None
    if _pool is None:
        database_url = os.environ["DATABASE_URL"]
        ssl = "require" if "rds.amazonaws.com" in database_url else None
        _pool = await asyncpg.create_pool(
            database_url,
            min_size=2,
            max_size=10,
            ssl=ssl,
        )
        _pool_loop = loop
    return _pool


async def close_pool() -> None:
    """Shut down the connection pool gracefully."""
    global _pool, _pool_loop
    if _pool is not None:
        await _pool.close()
        _pool = None
        _pool_loop = None


@asynccontextmanager
async def acquire():
    """Convenience context manager for a single connection."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        yield conn
