"""Tests for FastAPI API endpoints — auth, health, CRUD."""

import pytest
from unittest.mock import AsyncMock
from httpx import AsyncClient, ASGITransport

import jwt
from datetime import datetime, timedelta, timezone

TEST_JWT_SECRET = "test-secret-for-unit-tests"  # pragma: allowlist secret
TEST_APP_PASSWORD = "test-password-123"  # pragma: allowlist secret


@pytest.fixture(autouse=True)
def env_vars(monkeypatch):
    """Set required env vars for api.main module."""
    monkeypatch.setenv("JWT_SECRET", TEST_JWT_SECRET)
    monkeypatch.setenv("APP_PASSWORD", TEST_APP_PASSWORD)
    monkeypatch.setenv("SECURE_COOKIES", "false")


@pytest.fixture
def token():
    return jwt.encode(
        {"exp": datetime.now(timezone.utc) + timedelta(hours=1)},
        TEST_JWT_SECRET,
        algorithm="HS256",
    )


@pytest.fixture
def expired_token():
    return jwt.encode(
        {"exp": datetime.now(timezone.utc) - timedelta(hours=1)},
        TEST_JWT_SECRET,
        algorithm="HS256",
    )


@pytest.fixture
def mock_pool():
    """Mock asyncpg pool to avoid needing a real database."""
    pool = AsyncMock()
    return pool


@pytest.fixture
def fastapi_app(mock_pool, env_vars):
    # Force reimport so env vars are picked up
    import importlib
    import api.main

    importlib.reload(api.main)
    from api.main import app

    app.state._pool_override = mock_pool
    return app


@pytest.mark.asyncio
async def test_health_no_auth(fastapi_app):
    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_jobs_requires_auth(fastapi_app):
    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/jobs")
        assert resp.status_code == 401


@pytest.mark.asyncio
async def test_jobs_rejects_expired_token(fastapi_app, expired_token):
    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/jobs", cookies={"token": expired_token})
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Token expired"


@pytest.mark.asyncio
async def test_login_wrong_password(fastapi_app):
    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/login",
            json={"password": "wrong"},  # pragma: allowlist secret
        )
        assert resp.status_code == 401


@pytest.mark.asyncio
async def test_login_correct_password(fastapi_app):
    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/login", json={"password": TEST_APP_PASSWORD})
        assert resp.status_code == 200
        assert "token" in resp.cookies


@pytest.mark.asyncio
async def test_queue_removed_from_cloud(fastapi_app):
    """Queue endpoints removed from cloud API — PII boundary enforcement."""
    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/queue")
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_followups_requires_auth(fastapi_app):
    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/followups")
        assert resp.status_code == 401
