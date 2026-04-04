"""Tests for api.debug.health_checks — GREEN, YELLOW, RED for each check."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.debug.health_checks import (
    HealthStatus,
    check_analysis_poller,
    check_bedrock_kb,
    check_cross_boundary,
    check_eventbridge,
    check_lambda,
    check_rds,
    check_s3,
    check_sqs,
    run_all_checks,
)


@pytest.fixture
def mock_pool():
    pool = AsyncMock()
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value="on")
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])

    class _CM:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *a):
            pass

    pool.acquire = MagicMock(return_value=_CM())
    pool._conn = conn
    return pool


# -- RDS --


@pytest.mark.asyncio
async def test_check_rds_green(mock_pool):
    conn = mock_pool._conn
    conn.fetchval = AsyncMock(side_effect=["PostgreSQL 15.4", "on"])
    conn.fetch = AsyncMock(
        side_effect=[
            [{"relname": "jobs", "row_count": 100}],  # pg_stat_user_tables
        ]
    )
    conn.fetchrow = AsyncMock(
        return_value={
            "started_at": datetime.now(timezone.utc),
            "status": "completed",
            "event_type": "new_jd",
        }
    )

    with patch("api.debug.schema_sync.check_schema_match") as m:
        m.return_value = {
            "match": True,
            "missing_tables": [],
            "extra_tables": [],
            "column_mismatches": {},
        }
        result = await check_rds(mock_pool)
    assert result.status == HealthStatus.GREEN
    assert result.key_metric
    assert len(result.checks) >= 2


@pytest.mark.asyncio
async def test_check_rds_yellow(mock_pool):
    conn = mock_pool._conn
    conn.fetchval = AsyncMock(side_effect=["PostgreSQL 15.4", "on"])
    conn.fetch = AsyncMock(return_value=[{"relname": "jobs", "row_count": 10}])
    conn.fetchrow = AsyncMock(return_value=None)

    with patch("api.debug.schema_sync.check_schema_match") as m:
        m.return_value = {
            "match": False,
            "missing_tables": ["deadlines"],
            "extra_tables": [],
            "column_mismatches": {},
        }
        result = await check_rds(mock_pool)
    assert result.status == HealthStatus.YELLOW


@pytest.mark.asyncio
async def test_check_rds_red():
    pool = AsyncMock()

    class _Fail:
        async def __aenter__(self):
            raise ConnectionError("refused")

        async def __aexit__(self, *a):
            pass

    pool.acquire = MagicMock(return_value=_Fail())
    result = await check_rds(pool)
    assert result.status == HealthStatus.RED
    assert any(not c.passed for c in result.checks)


# -- S3 --


@pytest.mark.asyncio
async def test_check_s3_green():
    mock_client = MagicMock()
    mock_client.list_objects_v2 = MagicMock(
        return_value={
            "KeyCount": 5,
            "Contents": [
                {
                    "Key": "jds/abc.json",
                    "LastModified": datetime.now(timezone.utc),
                    "Size": 1024,
                }
            ],
        }
    )
    with patch("boto3.client", return_value=mock_client):
        result = await check_s3("test-bucket")
    assert result.status == HealthStatus.GREEN
    assert "5" in result.key_metric


@pytest.mark.asyncio
async def test_check_s3_yellow():
    mock_client = MagicMock()
    mock_client.list_objects_v2 = MagicMock(
        return_value={"KeyCount": 0, "Contents": []}
    )
    with patch("boto3.client", return_value=mock_client):
        result = await check_s3("test-bucket")
    assert result.status == HealthStatus.YELLOW


@pytest.mark.asyncio
async def test_check_s3_red():
    result = await check_s3("")
    assert result.status == HealthStatus.RED


# -- SQS --


@pytest.mark.asyncio
async def test_check_sqs_green():
    mock_client = MagicMock()
    mock_client.get_queue_url = MagicMock(return_value={"QueueUrl": "https://sqs/q"})
    mock_client.get_queue_attributes = MagicMock(
        return_value={
            "Attributes": {
                "ApproximateNumberOfMessages": "3",
                "ApproximateNumberOfMessagesNotVisible": "0",
            }
        }
    )
    with patch("boto3.client", return_value=mock_client):
        result = await check_sqs("test-queue")
    assert result.status == HealthStatus.GREEN


@pytest.mark.asyncio
async def test_check_sqs_yellow():
    mock_client = MagicMock()
    mock_client.get_queue_url = MagicMock(return_value={"QueueUrl": "https://sqs/q"})
    mock_client.get_queue_attributes = MagicMock(
        return_value={
            "Attributes": {
                "ApproximateNumberOfMessages": "500",
                "ApproximateNumberOfMessagesNotVisible": "0",
            }
        }
    )
    with patch("boto3.client", return_value=mock_client):
        result = await check_sqs("test-queue")
    assert result.status == HealthStatus.YELLOW


@pytest.mark.asyncio
async def test_check_sqs_red():
    with patch("boto3.client", side_effect=Exception("not found")):
        result = await check_sqs("bad")
    assert result.status == HealthStatus.RED


# -- Lambda --


@pytest.mark.asyncio
async def test_check_lambda_green():
    mock_lam = MagicMock()
    mock_lam.get_function_configuration = MagicMock(
        return_value={"LastModified": "2026-03-29T10:00:00Z"}
    )
    mock_cw = MagicMock()
    mock_cw.get_metric_statistics = MagicMock(
        side_effect=[{"Datapoints": [{"Sum": 100}]}, {"Datapoints": [{"Sum": 1}]}]
    )
    mock_logs = MagicMock()
    mock_logs.filter_log_events = MagicMock(return_value={"events": []})
    with patch("boto3.client", side_effect=[mock_lam, mock_cw, mock_logs]):
        result = await check_lambda("job-search-platform-fetch")
    assert result.status == HealthStatus.GREEN


@pytest.mark.asyncio
async def test_check_lambda_red():
    mock_lam = MagicMock()
    mock_lam.get_function_configuration = MagicMock(
        return_value={"LastModified": "2026-03-29"}
    )
    mock_cw = MagicMock()
    mock_cw.get_metric_statistics = MagicMock(
        side_effect=[{"Datapoints": []}, {"Datapoints": []}]
    )
    mock_logs = MagicMock()
    mock_logs.filter_log_events = MagicMock(return_value={"events": []})
    with patch("boto3.client", side_effect=[mock_lam, mock_cw, mock_logs]):
        result = await check_lambda("job-search-platform-fetch")
    assert result.status == HealthStatus.RED


# -- EventBridge --


@pytest.mark.asyncio
async def test_check_eventbridge_green():
    mock_client = MagicMock()
    mock_client.list_rules = MagicMock(
        return_value={
            "Rules": [
                {"Name": "r1", "State": "ENABLED", "ScheduleExpression": "rate(1 day)"}
            ]
        }
    )
    with patch("boto3.client", return_value=mock_client):
        result = await check_eventbridge("test")
    assert result.status == HealthStatus.GREEN


@pytest.mark.asyncio
async def test_check_eventbridge_yellow():
    mock_client = MagicMock()
    mock_client.list_rules = MagicMock(
        return_value={
            "Rules": [
                {"Name": "r1", "State": "ENABLED"},
                {"Name": "r2", "State": "DISABLED"},
            ]
        }
    )
    with patch("boto3.client", return_value=mock_client):
        result = await check_eventbridge("test")
    assert result.status == HealthStatus.YELLOW


@pytest.mark.asyncio
async def test_check_eventbridge_red():
    mock_client = MagicMock()
    mock_client.list_rules = MagicMock(return_value={"Rules": []})
    with patch("boto3.client", return_value=mock_client):
        result = await check_eventbridge("test")
    assert result.status == HealthStatus.RED


# -- Bedrock KB --


@pytest.mark.asyncio
async def test_check_bedrock_kb_green():
    mock_client = MagicMock()
    mock_client.get_knowledge_base = MagicMock(
        return_value={"knowledgeBase": {"name": "kb", "status": "ACTIVE"}}
    )
    mock_client.list_data_sources = MagicMock(
        return_value={"dataSourceSummaries": [{"dataSourceId": "ds1"}]}
    )
    with patch("boto3.client", return_value=mock_client):
        result = await check_bedrock_kb("kb-id")
    assert result.status == HealthStatus.GREEN


@pytest.mark.asyncio
async def test_check_bedrock_kb_red():
    result = await check_bedrock_kb("")
    assert result.status == HealthStatus.RED


# -- Analysis Poller --


@pytest.mark.asyncio
async def test_check_analysis_poller_green(mock_pool):
    conn = mock_pool._conn
    conn.fetchval = AsyncMock(side_effect=[None, 0])  # config, unanalyzed count
    conn.fetchrow = AsyncMock(
        return_value={
            "started_at": datetime.now(timezone.utc) - timedelta(hours=1),
            "status": "completed",
            "error": None,
        }
    )
    result = await check_analysis_poller(mock_pool)
    assert result.status == HealthStatus.GREEN


@pytest.mark.asyncio
async def test_check_analysis_poller_red(mock_pool):
    conn = mock_pool._conn
    conn.fetchval = AsyncMock(return_value="false")
    result = await check_analysis_poller(mock_pool)
    assert result.status == HealthStatus.RED


# -- Cross-boundary --


@pytest.mark.asyncio
async def test_check_cross_boundary_green(mock_pool):
    conn = mock_pool._conn
    conn.fetch = AsyncMock(
        return_value=[
            {
                "event_type": "ingest_status",
                "cnt": 5,
                "latest": datetime.now(timezone.utc),
            }
        ]
    )
    result = await check_cross_boundary(mock_pool)
    assert result.status == HealthStatus.GREEN


@pytest.mark.asyncio
async def test_check_cross_boundary_yellow(mock_pool):
    conn = mock_pool._conn
    conn.fetch = AsyncMock(return_value=[])
    result = await check_cross_boundary(mock_pool)
    assert result.status == HealthStatus.YELLOW


# -- run_all_checks --


@pytest.mark.asyncio
async def test_run_all_checks_never_raises(mock_pool):
    with patch("boto3.client", side_effect=Exception("AWS down")):
        result = await run_all_checks(mock_pool)
    assert "components" in result
    assert "overall" in result
    assert isinstance(result["components"], dict)


@pytest.mark.asyncio
async def test_run_all_checks_has_checks_field(mock_pool):
    with patch("boto3.client", side_effect=Exception("AWS down")):
        result = await run_all_checks(mock_pool)
    for comp in result["components"].values():
        assert "checks" in comp
        assert "key_metric" in comp
