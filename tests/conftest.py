"""Pytest configuration — shared fixtures and import path setup."""

import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

# Add project root to path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# Alias 'lambda' directory as 'lambda_' for imports since 'lambda' is a keyword

lambda_dir = ROOT / "lambda"
lambda_mod = types.ModuleType("lambda_")
lambda_mod.__path__ = [str(lambda_dir)]
sys.modules["lambda_"] = lambda_mod

for sub in ["fetch", "persist"]:
    sub_dir = lambda_dir / sub
    # Add sub-directory to sys.path so intra-package imports (e.g. adapter_registry) resolve
    if str(sub_dir) not in sys.path:
        sys.path.insert(0, str(sub_dir))
    sub_mod = types.ModuleType(f"lambda_.{sub}")
    sub_mod.__path__ = [str(sub_dir)]
    sys.modules[f"lambda_.{sub}"] = sub_mod


# ---------------------------------------------------------------------------
# Database fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_db_conn():
    """Mock asyncpg connection with common methods."""
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=True)
    conn.fetchrow = AsyncMock(return_value={"id": 1})
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock()
    return conn


@pytest.fixture
def mock_acquire(mock_db_conn):
    """Mock the acquire() async context manager from local.agents.shared.db."""

    class _AcquireCM:
        async def __aenter__(self):
            return mock_db_conn

        async def __aexit__(self, *args):
            pass

    return _AcquireCM()


# ---------------------------------------------------------------------------
# Payload fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_status_payload():
    return {"job_id": 1, "stage": "applied", "deadline": None}


@pytest.fixture
def sample_recommendation_payload():
    return {"company": "Anthropic", "role": "Software Engineer"}


@pytest.fixture
def sample_followup_payload():
    return {"job_id": 1, "urgency": "high", "action": "send_followup"}
