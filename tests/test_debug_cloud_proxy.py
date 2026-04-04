"""Tests for local.debug.cloud_proxy — cloud API proxy health checks."""

from unittest.mock import patch

import pytest

from api.debug.health_checks import HealthStatus

# All tests need CLOUD_API_URL set so the functions don't short-circuit
_URL_PATCH = patch(
    "local.debug.cloud_proxy.CLOUD_API_URL", "https://test-api.example.com"
)


@pytest.mark.asyncio
async def test_check_rds_via_api_green():
    from local.debug.cloud_proxy import check_rds_via_api

    with _URL_PATCH, patch("local.debug.cloud_proxy._api_get") as mock_get:
        mock_get.side_effect = [
            [{"id": 1}, {"id": 2}, {"id": 3}],
            [{"source": "s3", "metric_name": "jds_processed"}],
        ]
        result = await check_rds_via_api()

    assert result.status == HealthStatus.GREEN
    assert "3" in result.key_metric


@pytest.mark.asyncio
async def test_check_rds_via_api_not_configured():
    from local.debug.cloud_proxy import check_rds_via_api

    with patch("local.debug.cloud_proxy.CLOUD_API_URL", ""):
        result = await check_rds_via_api()

    assert result.status == HealthStatus.RED


@pytest.mark.asyncio
async def test_check_orchestration_green():
    from local.debug.cloud_proxy import check_orchestration_via_api

    with _URL_PATCH, patch("local.debug.cloud_proxy._api_get") as mock_get:
        mock_get.return_value = [
            {
                "event_type": "new_jd",
                "status": "completed",
                "started_at": "2026-03-29T20:00:00+00:00",
            },
            {
                "event_type": "new_jd",
                "status": "completed",
                "started_at": "2026-03-29T19:00:00+00:00",
            },
        ]
        result = await check_orchestration_via_api()

    assert result.status == HealthStatus.GREEN


@pytest.mark.asyncio
async def test_check_orchestration_not_configured():
    from local.debug.cloud_proxy import check_orchestration_via_api

    with patch("local.debug.cloud_proxy.CLOUD_API_URL", ""):
        result = await check_orchestration_via_api()

    assert result.status == HealthStatus.RED


@pytest.mark.asyncio
async def test_check_cross_boundary_green():
    from local.debug.cloud_proxy import check_cross_boundary_via_api

    with _URL_PATCH, patch("local.debug.cloud_proxy._api_get") as mock_get:
        mock_get.return_value = [
            {
                "event_type": "ingest_status",
                "status": "completed",
                "started_at": "2026-03-29T20:00:00+00:00",
            },
            {
                "event_type": "ingest_recommendation",
                "status": "completed",
                "started_at": "2026-03-29T19:00:00+00:00",
            },
            {
                "event_type": "new_jd",
                "status": "completed",
                "started_at": "2026-03-29T18:00:00+00:00",
            },
        ]
        result = await check_cross_boundary_via_api()

    assert result.status == HealthStatus.GREEN
    assert "2" in result.key_metric


@pytest.mark.asyncio
async def test_check_cross_boundary_yellow():
    from local.debug.cloud_proxy import check_cross_boundary_via_api

    with _URL_PATCH, patch("local.debug.cloud_proxy._api_get") as mock_get:
        mock_get.return_value = [
            {
                "event_type": "new_jd",
                "status": "completed",
                "started_at": "2026-03-29T18:00:00+00:00",
            },
        ]
        result = await check_cross_boundary_via_api()

    assert result.status == HealthStatus.YELLOW


@pytest.mark.asyncio
async def test_fetch_summary():
    from local.debug.cloud_proxy import fetch_summary

    with _URL_PATCH, patch("local.debug.cloud_proxy._api_get") as mock_get:
        mock_get.side_effect = [
            [{"id": 1}, {"id": 2}],
            [
                {"event_type": "ingest_status", "started_at": "2026-03-29T20:00:00"},
                {"event_type": "new_jd", "started_at": "2026-03-29T19:00:00"},
            ],
        ]
        result = await fetch_summary()

    assert result["jobs"] == 2
    assert result["last_ingest"] is not None
    assert result["last_analysis"] is not None


@pytest.mark.asyncio
async def test_fetch_component_runs():
    from local.debug.cloud_proxy import fetch_component_runs

    with _URL_PATCH, patch("local.debug.cloud_proxy._api_get") as mock_get:
        mock_get.return_value = [
            {"event_type": "new_jd", "agent_chain": ["jd_analyzer", "resume_matcher"]},
            {"event_type": "email_check", "agent_chain": ["email_classifier"]},
        ]
        result = await fetch_component_runs("jd_analyzer")

    assert len(result) == 1
    assert result[0]["event_type"] == "new_jd"
