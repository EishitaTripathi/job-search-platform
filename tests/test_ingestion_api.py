"""Tests for API ingestion endpoints — HMAC auth, payload handling, ops."""

import hashlib
import hmac
import json
import time

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from httpx import AsyncClient, ASGITransport

import jwt
from datetime import datetime, timedelta, timezone

TEST_JWT_SECRET = "test-secret-for-unit-tests"  # pragma: allowlist secret
TEST_APP_PASSWORD = "test-password-123"  # pragma: allowlist secret
TEST_HMAC_KEY = "test-hmac-key-for-unit-tests"  # pragma: allowlist secret


@pytest.fixture(autouse=True)
def env_vars(monkeypatch):
    """Set required env vars for api.main module."""
    monkeypatch.setenv("JWT_SECRET", TEST_JWT_SECRET)
    monkeypatch.setenv("APP_PASSWORD", TEST_APP_PASSWORD)
    monkeypatch.setenv("SECURE_COOKIES", "false")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://test:test@localhost:5432/test",  # pragma: allowlist secret
    )
    monkeypatch.setenv("INGEST_HMAC_KEY", TEST_HMAC_KEY)


@pytest.fixture
def token():
    return jwt.encode(
        {"exp": datetime.now(timezone.utc) + timedelta(hours=1)},
        TEST_JWT_SECRET,
        algorithm="HS256",
    )


class _FakeAcquire:
    """Async context manager that mimics asyncpg pool.acquire()."""

    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *args):
        pass


@pytest.fixture
def mock_pool():
    """Mock asyncpg pool to avoid needing a real database."""
    pool = MagicMock()
    return pool


@pytest.fixture
def fastapi_app(mock_pool):
    import importlib
    import api.main

    importlib.reload(api.main)

    # Reload iam_auth so the INGEST_HMAC_KEY env var is picked up
    import api.iam_auth

    importlib.reload(api.iam_auth)
    # Re-patch the dependency in api.main
    api.main.require_hmac_auth = api.iam_auth.require_hmac_auth

    from api.main import app

    app.state._pool_override = mock_pool
    return app


def _hmac_headers(body_bytes: bytes) -> dict:
    """Generate valid HMAC authentication headers."""
    ts = str(int(time.time()))
    message = f"{ts}.{body_bytes.decode()}"
    sig = hmac.new(
        TEST_HMAC_KEY.encode(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()
    return {"X-Signature": sig, "X-Timestamp": ts}


# ---------------------------------------------------------------------------
# Ingestion: /api/ingest/status
# ---------------------------------------------------------------------------


class TestIngestStatus:
    @pytest.mark.asyncio
    async def test_accepts_valid_payload(self, fastapi_app, mock_pool):
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value={"id": 1})
        mock_conn.execute = AsyncMock()
        mock_pool.acquire.return_value = _FakeAcquire(mock_conn)

        import api.main

        api.main._pool = mock_pool

        body = json.dumps({"job_id": 1, "stage": "applied"}).encode()
        headers = _hmac_headers(body)
        headers["Content-Type"] = "application/json"

        transport = ASGITransport(app=fastapi_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/ingest/status",
                content=body,
                headers=headers,
            )

        assert resp.status_code == 200
        assert resp.json()["status"] == "ingested"

    @pytest.mark.asyncio
    async def test_rejects_without_hmac(self, fastapi_app):
        transport = ASGITransport(app=fastapi_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/ingest/status",
                json={"job_id": 1, "stage": "applied"},
            )

        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Ingestion: /api/ingest/recommendation
# ---------------------------------------------------------------------------


class TestIngestRecommendation:
    @pytest.mark.asyncio
    async def test_creates_job_record(self, fastapi_app, mock_pool):
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value={"id": 42})
        mock_pool.acquire.return_value = _FakeAcquire(mock_conn)

        import api.main

        api.main._pool = mock_pool

        body = json.dumps({"company": "Anthropic", "role": "SWE"}).encode()
        headers = _hmac_headers(body)
        headers["Content-Type"] = "application/json"

        transport = ASGITransport(app=fastapi_app)
        with patch.object(
            api.main, "_enqueue_jd_fetch", return_value=True
        ) as mock_enqueue:
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/ingest/recommendation",
                    content=body,
                    headers=headers,
                )

        assert resp.status_code == 200
        data = resp.json()
        assert data["type"] == "recommendation"
        assert data["job_id"] == 42
        mock_enqueue.assert_called_once_with(42, "Anthropic", "SWE")

    @pytest.mark.asyncio
    async def test_sqs_enqueue_skipped_on_duplicate(self, fastapi_app, mock_pool):
        """Duplicate job (ON CONFLICT DO NOTHING) → no SQS enqueue."""
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value=None)  # dedup — no new row
        mock_pool.acquire.return_value = _FakeAcquire(mock_conn)

        import api.main

        api.main._pool = mock_pool

        body = json.dumps({"company": "Anthropic", "role": "SWE"}).encode()
        headers = _hmac_headers(body)
        headers["Content-Type"] = "application/json"

        transport = ASGITransport(app=fastapi_app)
        with patch.object(api.main, "_enqueue_jd_fetch") as mock_enqueue:
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/ingest/recommendation",
                    content=body,
                    headers=headers,
                )

        assert resp.status_code == 200
        assert resp.json()["job_id"] is None
        mock_enqueue.assert_not_called()

    @pytest.mark.asyncio
    async def test_sqs_failure_does_not_block_response(self, fastapi_app, mock_pool):
        """SQS enqueue failure → endpoint still returns 200."""
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value={"id": 99})
        mock_pool.acquire.return_value = _FakeAcquire(mock_conn)

        import api.main

        api.main._pool = mock_pool

        body = json.dumps({"company": "Stripe", "role": "Backend"}).encode()
        headers = _hmac_headers(body)
        headers["Content-Type"] = "application/json"

        transport = ASGITransport(app=fastapi_app)
        with patch.object(api.main, "_enqueue_jd_fetch", return_value=False):
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/ingest/recommendation",
                    content=body,
                    headers=headers,
                )

        assert resp.status_code == 200
        assert resp.json()["job_id"] == 99


# ---------------------------------------------------------------------------
# Ingestion: /api/ingest/followup
# ---------------------------------------------------------------------------


class TestIngestFollowup:
    @pytest.mark.asyncio
    async def test_creates_followup(self, fastapi_app, mock_pool):
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value={"id": 1})
        mock_conn.execute = AsyncMock()
        mock_pool.acquire.return_value = _FakeAcquire(mock_conn)

        import api.main

        api.main._pool = mock_pool

        body = json.dumps(
            {
                "job_id": 1,
                "urgency": "high",
                "action": "send_followup",
            }
        ).encode()
        headers = _hmac_headers(body)
        headers["Content-Type"] = "application/json"

        transport = ASGITransport(app=fastapi_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/ingest/followup",
                content=body,
                headers=headers,
            )

        assert resp.status_code == 200
        assert resp.json()["type"] == "followup"


# ---------------------------------------------------------------------------
# Dashboard endpoints: /api/deadlines, /api/ops/metrics
# ---------------------------------------------------------------------------


class TestDashboardEndpoints:
    @pytest.mark.asyncio
    async def test_deadlines_returns_list(self, fastapi_app, mock_pool, token):
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_pool.acquire.return_value = _FakeAcquire(mock_conn)

        import api.main

        api.main._pool = mock_pool

        transport = ASGITransport(app=fastapi_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/deadlines",
                cookies={"token": token},
            )

        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    @pytest.mark.asyncio
    async def test_ops_metrics_returns_list(self, fastapi_app, mock_pool, token):
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_pool.acquire.return_value = _FakeAcquire(mock_conn)

        import api.main

        api.main._pool = mock_pool

        transport = ASGITransport(app=fastapi_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/ops/metrics",
                cookies={"token": token},
            )

        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    @pytest.mark.asyncio
    async def test_deadlines_requires_auth(self, fastapi_app):
        transport = ASGITransport(app=fastapi_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/deadlines")

        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_ops_metrics_requires_auth(self, fastapi_app):
        transport = ASGITransport(app=fastapi_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/ops/metrics")

        assert resp.status_code == 401
