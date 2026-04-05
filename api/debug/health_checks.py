"""Cloud health checks for the debug dashboard.

Each ``check_*`` function probes one infrastructure component and returns a
:class:`HealthResult` with:
- status (GREEN/YELLOW/RED)
- expected vs actual comparisons
- key_metric (one-liner for graph node display)
- raw API response data (collapsed in UI)

:func:`run_all_checks` executes them concurrently with per-check timeouts
and is guaranteed never to raise.
"""

from __future__ import annotations

import asyncio
import json
import os
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema path — works both in repo checkout and inside Docker container
# ---------------------------------------------------------------------------
_SCHEMA_PATH = Path(__file__).parent.parent.parent / "infra" / "schema.sql"
if not _SCHEMA_PATH.exists():
    _SCHEMA_PATH = Path("/app/infra/schema.sql")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_CHECK_TIMEOUT = 10  # seconds per individual check

S3_BUCKET = os.environ.get("S3_BUCKET", "")
SQS_QUEUE_NAME = os.environ.get("SQS_QUEUE_NAME", "job-search-platform-jd-scrape-queue")
AWS_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-2")
BEDROCK_KB_ID = os.environ.get("BEDROCK_KB_ID", "")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
class HealthStatus(str, Enum):
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


@dataclass
class ExpectedActual:
    check: str
    expected: str
    actual: str
    passed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "expected": self.expected,
            "actual": self.actual,
            "passed": self.passed,
        }


@dataclass
class HealthResult:
    component: str
    status: HealthStatus
    message: str
    key_metric: str = ""
    checks: list[ExpectedActual] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_activity: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        d["checked_at"] = self.checked_at.isoformat()
        d["checks"] = [c.to_dict() for c in self.checks]
        return d


# ---------------------------------------------------------------------------
# Helper: worst status
# ---------------------------------------------------------------------------
_STATUS_ORDER = {HealthStatus.GREEN: 0, HealthStatus.YELLOW: 1, HealthStatus.RED: 2}


def _worst(statuses: list[HealthStatus]) -> HealthStatus:
    if not statuses:
        return HealthStatus.RED
    return max(statuses, key=lambda s: _STATUS_ORDER[s])


# ---------------------------------------------------------------------------
# CloudWatch log fetcher — filtered error logs for any component
# ---------------------------------------------------------------------------
async def fetch_error_logs(
    log_group: str,
    filter_pattern: str = "",
    limit: int = 10,
    region: str | None = None,
) -> list[dict[str, str]]:
    """Fetch recent error/warning log lines from CloudWatch.

    Tries the given filter_pattern first. If no results, falls back to
    broader error patterns, then to the last N log lines as context.
    """
    rgn = region or AWS_REGION

    try:
        import boto3

        def _fetch():
            client = boto3.client("logs", region_name=rgn)

            # Try patterns in order: specific → broad → all
            patterns_to_try = []
            if filter_pattern:
                patterns_to_try.append(filter_pattern)
            patterns_to_try.extend(
                [
                    '?ERROR ?Error ?Exception ?Traceback ?FAILED ?"Task timed out"',
                    "",  # fallback: last N lines (no filter)
                ]
            )

            for pattern in patterns_to_try:
                kwargs = {
                    "logGroupName": log_group,
                    "limit": limit,
                    "interleaved": True,
                }
                if pattern:
                    kwargs["filterPattern"] = pattern

                resp = client.filter_log_events(**kwargs)
                events = resp.get("events", [])
                if events:
                    results = []
                    for event in events:
                        msg = event["message"].strip()
                        # Skip boring Lambda runtime lines
                        if msg.startswith(("START ", "END ", "REPORT ", "INIT_START")):
                            continue
                        results.append(
                            {
                                "timestamp": datetime.fromtimestamp(
                                    event["timestamp"] / 1000, tz=timezone.utc
                                ).isoformat(),
                                "message": msg[:1000],
                                "is_error": bool(
                                    pattern
                                ),  # True if matched an error filter
                            }
                        )
                    if results:
                        return results

            return []

        return await asyncio.to_thread(_fetch)
    except Exception:
        logger.exception("Failed to fetch logs from %s", log_group)
        return []


# Map component IDs to CloudWatch log groups and filter patterns
_COMPONENT_LOG_CONFIG: dict[str, dict[str, str]] = {
    # Lambda functions have their own log groups
    "lambda_fetch": {
        "log_group": "/aws/lambda/job-search-platform-fetch",
        "error_filter": "",  # broad search — fetch_error_logs tries error patterns then falls back to recent
    },
    "lambda_persist": {
        "log_group": "/aws/lambda/job-search-platform-persist",
        "error_filter": "",
    },
    # All ECS components share one log group — filter by component-specific keywords
    "cloud_coordinator": {
        "log_group": "/ecs/job-search-platform",
        "error_filter": '?"Cloud Coordinator" ?"dispatch failed" ?"cloud_coordinator"',
    },
    "jd_analyzer": {
        "log_group": "/ecs/job-search-platform",
        "error_filter": '?"JD Analyzer" ?"jd_analyzer" ?"strip_boilerplate" ?"extract_fields"',
    },
    "sponsorship_screener": {
        "log_group": "/ecs/job-search-platform",
        "error_filter": '?"Sponsorship" ?"sponsorship_screener"',
    },
    "resume_matcher": {
        "log_group": "/ecs/job-search-platform",
        "error_filter": '?"Resume Matcher" ?"resume_matcher" ?"rerank"',
    },
    "application_chat": {
        "log_group": "/ecs/job-search-platform",
        "error_filter": '?"Application Chat" ?"application_chat"',
    },
    "analysis_poller": {
        "log_group": "/ecs/job-search-platform",
        "error_filter": '?"Analysis polling" ?"poll_unanalyzed" ?"analysis_poller"',
    },
    "cloud_ingest_api": {
        "log_group": "/ecs/job-search-platform",
        "error_filter": '?"ingest" ?"HMAC"',
    },
    # SQS doesn't have its own logs but Lambda Fetch processes SQS messages
    "sqs": {
        "log_group": "/aws/lambda/job-search-platform-fetch",
        "error_filter": "",
    },
    # Catch-all for any ECS component
    "ecs": {
        "log_group": "/ecs/job-search-platform",
        "error_filter": "",
    },
}


async def fetch_component_error_logs(
    component_id: str, limit: int = 10
) -> list[dict[str, str]]:
    """Fetch filtered error logs for a specific component."""
    config = _COMPONENT_LOG_CONFIG.get(component_id)
    if not config:
        return []
    return await fetch_error_logs(
        log_group=config["log_group"],
        filter_pattern=config["error_filter"],
        limit=limit,
    )


def _ago(dt: datetime | None) -> str:
    """Format a datetime as 'Xm ago', 'Xh ago', etc."""
    if not dt:
        return "never"
    diff = datetime.now(timezone.utc) - dt
    mins = int(diff.total_seconds() / 60)
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins}m ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


# ---------------------------------------------------------------------------
# 1. RDS
# ---------------------------------------------------------------------------
async def check_rds(pool) -> HealthResult:
    try:
        async with pool.acquire() as conn:
            version = await conn.fetchval("SELECT version()")
            ssl_in_use = await conn.fetchval("SHOW ssl")
            ssl_ok = ssl_in_use == "on"

            # Row counts per table
            table_rows = await conn.fetch(
                "SELECT relname, n_live_tup::bigint AS row_count "
                "FROM pg_stat_user_tables ORDER BY n_live_tup DESC"
            )
            row_counts = {r["relname"]: r["row_count"] for r in table_rows}
            jobs_count = row_counts.get("jobs", 0)

            # Schema sync
            from api.debug.schema_sync import check_schema_match

            schema_result = await check_schema_match(conn, _SCHEMA_PATH)
            schema_ok = schema_result["match"]
            table_count = len(row_counts)
            expected_tables = 13

            # Last orchestration run
            last_run = await conn.fetchrow(
                "SELECT started_at, status, event_type FROM orchestration_runs "
                "ORDER BY started_at DESC LIMIT 1"
            )
            last_run_ago = _ago(last_run["started_at"]) if last_run else "never"

            checks = [
                ExpectedActual(
                    "Schema match",
                    f"{expected_tables} tables matching schema.sql",
                    f"{table_count} tables"
                    + (
                        ""
                        if schema_ok
                        else f" (missing: {', '.join(schema_result['missing_tables'])})"
                    ),
                    schema_ok,
                ),
                ExpectedActual("SSL", "on", ssl_in_use or "off", ssl_ok),
                ExpectedActual("Connection", "active", "active", True),
            ]

            key_parts = [f"{jobs_count} jobs"]
            key_parts.append("schema ✓" if schema_ok else "schema ✗")
            key_metric = " · ".join(key_parts)

            status = HealthStatus.GREEN
            if not schema_ok:
                status = HealthStatus.YELLOW
            if not ssl_ok and "rds.amazonaws.com" in os.environ.get("DATABASE_URL", ""):
                status = HealthStatus.YELLOW

            return HealthResult(
                component="rds",
                status=status,
                message=f"Connected, {table_count} tables, {jobs_count} jobs",
                key_metric=key_metric,
                checks=checks,
                details={
                    "row_counts": row_counts,
                    "schema": schema_result,
                    "last_run": last_run_ago,
                },
                raw={
                    "version": version,
                    "ssl": ssl_in_use,
                    "pg_stat_user_tables": row_counts,
                    "schema_diff": schema_result,
                },
                last_activity=last_run_ago,
            )
    except Exception as exc:
        logger.exception("RDS health check failed")
        return HealthResult(
            component="rds",
            status=HealthStatus.RED,
            message=f"Connection failed: {exc}",
            key_metric="disconnected",
            checks=[ExpectedActual("Connection", "active", str(exc), False)],
        )


# ---------------------------------------------------------------------------
# 2. S3
# ---------------------------------------------------------------------------
async def check_s3(bucket_name: str | None = None) -> HealthResult:
    bucket = bucket_name or S3_BUCKET
    if not bucket:
        return HealthResult(
            component="s3",
            status=HealthStatus.RED,
            message="S3_BUCKET not configured",
            key_metric="not configured",
            checks=[
                ExpectedActual(
                    "Bucket configured", "S3_BUCKET env set", "not set", False
                )
            ],
        )

    try:
        import boto3

        def _check():
            client = boto3.client("s3", region_name=AWS_REGION)
            # Count objects under jds/
            resp = client.list_objects_v2(Bucket=bucket, Prefix="jds/", MaxKeys=100)
            count = resp.get("KeyCount", 0)
            contents = resp.get("Contents", [])

            # Last object
            last_obj = None
            if contents:
                sorted_objs = sorted(
                    contents, key=lambda x: x.get("LastModified", ""), reverse=True
                )
                lo = sorted_objs[0]
                last_obj = {
                    "key": lo["Key"],
                    "modified": lo["LastModified"].isoformat(),
                    "size": lo["Size"],
                }

            raw_resp = {"KeyCount": count, "Bucket": bucket, "Prefix": "jds/"}
            if last_obj:
                raw_resp["LastObject"] = last_obj

            return count, last_obj, raw_resp

        count, last_obj, raw_resp = await asyncio.to_thread(_check)

        last_str = "none"
        if last_obj:
            last_dt = datetime.fromisoformat(last_obj["modified"])
            last_str = _ago(last_dt)

        checks = [
            ExpectedActual(
                "Bucket accessible", f"bucket '{bucket}' accessible", "accessible", True
            ),
            ExpectedActual(
                "JDs stored", "jds/ prefix has objects", f"{count} objects", count > 0
            ),
        ]
        if last_obj:
            checks.append(
                ExpectedActual(
                    "Recent JD", "recent JD uploads", f"last: {last_str}", True
                )
            )

        status = HealthStatus.GREEN if count > 0 else HealthStatus.YELLOW
        return HealthResult(
            component="s3",
            status=status,
            message=f"{count} JDs stored, last: {last_str}",
            key_metric=f"{count} JDs stored",
            checks=checks,
            details={"bucket": bucket, "jd_count": count, "last_object": last_obj},
            raw={"list_objects_v2": raw_resp},
        )
    except Exception as exc:
        logger.exception("S3 health check failed")
        return HealthResult(
            component="s3",
            status=HealthStatus.RED,
            message=f"Inaccessible: {exc}",
            key_metric="inaccessible",
            checks=[ExpectedActual("Bucket accessible", "accessible", str(exc), False)],
            raw={"error": str(exc)},
        )


# ---------------------------------------------------------------------------
# 3. SQS
# ---------------------------------------------------------------------------
async def check_sqs(
    queue_name: str | None = None, region: str | None = None
) -> HealthResult:
    q_name = queue_name or SQS_QUEUE_NAME
    rgn = region or AWS_REGION

    try:
        import boto3
        from botocore.exceptions import ClientError

        def _check():
            client = boto3.client("sqs", region_name=rgn)
            url_resp = client.get_queue_url(QueueName=q_name)
            queue_url = url_resp["QueueUrl"]

            attrs = client.get_queue_attributes(
                QueueUrl=queue_url,
                AttributeNames=["All"],
            )["Attributes"]

            depth = int(attrs.get("ApproximateNumberOfMessages", 0))
            in_flight = int(attrs.get("ApproximateNumberOfMessagesNotVisible", 0))

            # Check DLQ
            dlq_count = 0
            dlq_raw = {}
            redrive = attrs.get("RedrivePolicy")
            if redrive:
                rp = json.loads(redrive)
                dlq_arn = rp.get("deadLetterTargetArn", "")
                if dlq_arn:
                    dlq_name = dlq_arn.split(":")[-1]
                    try:
                        dlq_url = client.get_queue_url(QueueName=dlq_name)["QueueUrl"]
                        dlq_attrs = client.get_queue_attributes(
                            QueueUrl=dlq_url,
                            AttributeNames=["ApproximateNumberOfMessages"],
                        )["Attributes"]
                        dlq_count = int(dlq_attrs.get("ApproximateNumberOfMessages", 0))
                        dlq_raw = dlq_attrs
                    except ClientError:
                        pass

            return depth, in_flight, dlq_count, attrs, dlq_raw

        depth, in_flight, dlq_count, raw_attrs, dlq_raw = await asyncio.to_thread(
            _check
        )

        checks = [
            ExpectedActual("Queue exists", f"queue '{q_name}' exists", "exists", True),
            ExpectedActual(
                "Queue depth", "< 100 messages", f"{depth} messages", depth < 100
            ),
            ExpectedActual(
                "DLQ empty",
                "0 messages in DLQ",
                f"{dlq_count} messages",
                dlq_count == 0,
            ),
        ]

        status = HealthStatus.GREEN
        if depth > 100 or dlq_count > 0:
            status = HealthStatus.YELLOW

        return HealthResult(
            component="sqs",
            status=status,
            message=f"depth: {depth}, in-flight: {in_flight}, DLQ: {dlq_count}",
            key_metric=f"depth: {depth} · DLQ: {dlq_count}",
            checks=checks,
            details={
                "queue_name": q_name,
                "depth": depth,
                "in_flight": in_flight,
                "dlq_count": dlq_count,
            },
            raw={"get_queue_attributes": raw_attrs, "dlq_attributes": dlq_raw},
        )
    except Exception as exc:
        logger.exception("SQS health check failed")
        return HealthResult(
            component="sqs",
            status=HealthStatus.RED,
            message=f"Queue not found: {exc}",
            key_metric="not found",
            checks=[ExpectedActual("Queue exists", "exists", str(exc), False)],
            raw={"error": str(exc)},
        )


# ---------------------------------------------------------------------------
# 4. Lambda
# ---------------------------------------------------------------------------
async def check_lambda(function_name: str, region: str | None = None) -> HealthResult:
    rgn = region or AWS_REGION
    component_id = function_name.replace("job-search-platform-", "lambda_")

    try:
        import boto3

        def _check():
            lam = boto3.client("lambda", region_name=rgn)
            config = lam.get_function_configuration(FunctionName=function_name)

            cw = boto3.client("cloudwatch", region_name=rgn)
            now = datetime.now(timezone.utc)
            start = now - timedelta(hours=24)

            invocations = cw.get_metric_statistics(
                Namespace="AWS/Lambda",
                MetricName="Invocations",
                Dimensions=[{"Name": "FunctionName", "Value": function_name}],
                StartTime=start,
                EndTime=now,
                Period=86400,
                Statistics=["Sum"],
            )
            errors = cw.get_metric_statistics(
                Namespace="AWS/Lambda",
                MetricName="Errors",
                Dimensions=[{"Name": "FunctionName", "Value": function_name}],
                StartTime=start,
                EndTime=now,
                Period=86400,
                Statistics=["Sum"],
            )

            total_inv = int(sum(dp["Sum"] for dp in invocations.get("Datapoints", [])))
            total_err = int(sum(dp["Sum"] for dp in errors.get("Datapoints", [])))
            error_rate = (total_err / total_inv * 100) if total_inv > 0 else 0.0

            # Get last 5 CloudWatch log lines
            log_lines = []
            try:
                logs_client = boto3.client("logs", region_name=rgn)
                log_group = f"/aws/lambda/{function_name}"
                log_resp = logs_client.filter_log_events(
                    logGroupName=log_group,
                    limit=5,
                    interleaved=True,
                )
                for event in log_resp.get("events", []):
                    log_lines.append(
                        {
                            "timestamp": datetime.fromtimestamp(
                                event["timestamp"] / 1000, tz=timezone.utc
                            ).isoformat(),
                            "message": event["message"].strip()[:500],
                        }
                    )
            except Exception:
                pass  # Log access may fail — non-fatal

            raw_config = {
                "FunctionName": config.get("FunctionName"),
                "Runtime": config.get("Runtime"),
                "LastModified": config.get("LastModified", ""),
                "MemorySize": config.get("MemorySize"),
                "Timeout": config.get("Timeout"),
            }

            return total_inv, total_err, error_rate, raw_config, log_lines

        (
            total_inv,
            total_err,
            error_rate,
            raw_config,
            log_lines,
        ) = await asyncio.to_thread(_check)

        checks = [
            ExpectedActual("Function exists", "function deployed", "deployed", True),
            ExpectedActual(
                "Invocations (24h)",
                "> 0 invocations",
                f"{total_inv} invocations",
                total_inv > 0,
            ),
            ExpectedActual("Error rate", "< 5%", f"{error_rate:.1f}%", error_rate < 5),
        ]

        if total_inv == 0:
            status = HealthStatus.RED
        elif error_rate > 5:
            status = HealthStatus.YELLOW
        else:
            status = HealthStatus.GREEN

        return HealthResult(
            component=component_id,
            status=status,
            message=f"{total_inv} invocations, {error_rate:.1f}% errors in 24h",
            key_metric=f"24h: {total_inv} inv · {error_rate:.0f}% err",
            checks=checks,
            details={
                "invocations_24h": total_inv,
                "errors_24h": total_err,
                "error_rate_pct": round(error_rate, 2),
                "recent_logs": log_lines,
            },
            raw={
                "get_function_configuration": raw_config,
                "cloudwatch_invocations_24h": total_inv,
                "cloudwatch_errors_24h": total_err,
                "recent_log_events": log_lines,
            },
        )
    except Exception as exc:
        logger.exception("Lambda health check failed for %s", function_name)
        return HealthResult(
            component=component_id,
            status=HealthStatus.RED,
            message=f"Function missing or inaccessible: {exc}",
            key_metric="inaccessible",
            checks=[ExpectedActual("Function exists", "deployed", str(exc), False)],
            raw={"error": str(exc)},
        )


# ---------------------------------------------------------------------------
# 5. EventBridge
# ---------------------------------------------------------------------------
async def check_eventbridge(
    rule_prefix: str, region: str | None = None
) -> HealthResult:
    rgn = region or AWS_REGION

    try:
        import boto3

        def _check():
            client = boto3.client("events", region_name=rgn)
            resp = client.list_rules(NamePrefix=rule_prefix)
            rules = resp.get("Rules", [])
            return [
                {
                    "name": r["Name"],
                    "state": r.get("State", "UNKNOWN"),
                    "schedule": r.get("ScheduleExpression", ""),
                    "arn": r.get("Arn", ""),
                }
                for r in rules
            ]

        rules = await asyncio.to_thread(_check)

        if not rules:
            return HealthResult(
                component="eventbridge",
                status=HealthStatus.RED,
                message=f"No rules found with prefix '{rule_prefix}'",
                key_metric="no rules",
                checks=[
                    ExpectedActual(
                        "Rules exist",
                        f"rules with prefix '{rule_prefix}'",
                        "none found",
                        False,
                    )
                ],
                raw={"list_rules": []},
            )

        enabled = [r for r in rules if r["state"] == "ENABLED"]
        disabled = [r for r in rules if r["state"] != "ENABLED"]

        checks = [
            ExpectedActual(
                "Rules exist",
                "EventBridge rules configured",
                f"{len(rules)} rules found",
                True,
            ),
            ExpectedActual(
                "All rules enabled",
                f"all {len(rules)} rules enabled",
                f"{len(enabled)}/{len(rules)} enabled"
                + (
                    f" ({', '.join(r['name'] + ' DISABLED' for r in disabled)})"
                    if disabled
                    else ""
                ),
                len(disabled) == 0,
            ),
        ]
        for r in rules:
            checks.append(
                ExpectedActual(
                    r["name"],
                    "ENABLED",
                    r["state"] + (f" ({r['schedule']})" if r["schedule"] else ""),
                    r["state"] == "ENABLED",
                )
            )

        status = HealthStatus.GREEN if not disabled else HealthStatus.YELLOW
        return HealthResult(
            component="eventbridge",
            status=status,
            message=f"{len(enabled)}/{len(rules)} rules enabled",
            key_metric=f"{len(enabled)}/{len(rules)} rules enabled",
            checks=checks,
            details={"rules": rules},
            raw={"list_rules": rules},
        )
    except Exception as exc:
        logger.exception("EventBridge health check failed")
        return HealthResult(
            component="eventbridge",
            status=HealthStatus.RED,
            message=f"Inaccessible: {exc}",
            key_metric="inaccessible",
            checks=[ExpectedActual("Access", "accessible", str(exc), False)],
            raw={"error": str(exc)},
        )


# ---------------------------------------------------------------------------
# 6. Bedrock Knowledge Base
# ---------------------------------------------------------------------------
async def check_bedrock_kb(
    kb_id: str | None = None, region: str | None = None
) -> HealthResult:
    kid = kb_id or BEDROCK_KB_ID
    rgn = region or AWS_REGION

    if not kid:
        return HealthResult(
            component="bedrock_kb",
            status=HealthStatus.RED,
            message="BEDROCK_KB_ID not configured",
            key_metric="not configured",
            checks=[
                ExpectedActual(
                    "KB configured", "BEDROCK_KB_ID env set", "not set", False
                )
            ],
        )

    try:
        import boto3

        def _check():
            client = boto3.client("bedrock-agent", region_name=rgn)
            kb = client.get_knowledge_base(knowledgeBaseId=kid)
            kb_info = kb.get("knowledgeBase", {})
            ds_resp = client.list_data_sources(knowledgeBaseId=kid)
            data_sources = ds_resp.get("dataSourceSummaries", [])
            return kb_info, data_sources

        kb_info, data_sources = await asyncio.to_thread(_check)

        kb_status = kb_info.get("status", "UNKNOWN")
        checks = [
            ExpectedActual(
                "KB accessible",
                "knowledge base reachable",
                f"status: {kb_status}",
                True,
            ),
            ExpectedActual(
                "Data sources",
                ">= 1 data source",
                f"{len(data_sources)} data source(s)",
                len(data_sources) > 0,
            ),
        ]

        status = HealthStatus.GREEN if data_sources else HealthStatus.YELLOW
        return HealthResult(
            component="bedrock_kb",
            status=status,
            message=f"KB {kb_status}, {len(data_sources)} data source(s)",
            key_metric=f"{kb_status} · {len(data_sources)} source(s)",
            checks=checks,
            details={
                "kb_id": kid,
                "name": kb_info.get("name", ""),
                "status": kb_status,
                "data_source_count": len(data_sources),
            },
            raw={
                "get_knowledge_base": {
                    "name": kb_info.get("name"),
                    "status": kb_status,
                    "knowledgeBaseId": kid,
                },
                "list_data_sources": [
                    {
                        "id": ds.get("dataSourceId"),
                        "name": ds.get("name"),
                        "status": ds.get("status"),
                    }
                    for ds in data_sources
                ],
            },
        )
    except Exception as exc:
        logger.exception("Bedrock KB health check failed")
        return HealthResult(
            component="bedrock_kb",
            status=HealthStatus.RED,
            message=f"Inaccessible: {exc}",
            key_metric="inaccessible",
            checks=[ExpectedActual("KB accessible", "reachable", str(exc), False)],
            raw={"error": str(exc)},
        )


# ---------------------------------------------------------------------------
# 7. Analysis Poller
# ---------------------------------------------------------------------------
async def check_analysis_poller(pool) -> HealthResult:
    try:
        async with pool.acquire() as conn:
            flag = await conn.fetchval(
                "SELECT value FROM config WHERE key = $1",
                "analysis_polling_enabled",
            )
            enabled = flag is None or flag.lower() != "false"

            if not enabled:
                return HealthResult(
                    component="analysis_poller",
                    status=HealthStatus.RED,
                    message="Polling disabled in config",
                    key_metric="disabled",
                    checks=[
                        ExpectedActual(
                            "Enabled",
                            "analysis_polling_enabled = true",
                            f"config value = '{flag}'",
                            False,
                        )
                    ],
                    raw={"config_value": flag},
                )

            last_run = await conn.fetchrow(
                "SELECT started_at, status, error FROM orchestration_runs "
                "WHERE event_type = $1 ORDER BY started_at DESC LIMIT 1",
                "new_jd",
            )

            # Count unanalyzed jobs
            unanalyzed = await conn.fetchval(
                "SELECT COUNT(*) FROM jobs j "
                "LEFT JOIN jd_analyses ja ON ja.job_id = j.id "
                "WHERE j.jd_s3_key IS NOT NULL AND ja.id IS NULL"
            )

            checks = [
                ExpectedActual("Enabled", "polling enabled", "enabled", True),
                ExpectedActual(
                    "Unanalyzed jobs",
                    "0 pending",
                    f"{unanalyzed} pending",
                    unanalyzed == 0,
                ),
            ]

            if not last_run:
                checks.append(
                    ExpectedActual(
                        "Recent activity", "runs in last 6h", "no runs found", False
                    )
                )
                return HealthResult(
                    component="analysis_poller",
                    status=HealthStatus.YELLOW,
                    message="Enabled but no runs found",
                    key_metric=f"no runs · {unanalyzed} pending",
                    checks=checks,
                    details={"enabled": True, "unanalyzed": unanalyzed},
                    raw={"config_value": flag, "unanalyzed_count": unanalyzed},
                )

            last_started = last_run["started_at"]
            age = datetime.now(timezone.utc) - last_started
            age_str = _ago(last_started)

            checks.append(
                ExpectedActual(
                    "Recent activity",
                    "run within last 6h",
                    f"last run: {age_str} ({last_run['status']})",
                    age < timedelta(hours=6),
                )
            )

            status = (
                HealthStatus.GREEN if age < timedelta(hours=6) else HealthStatus.YELLOW
            )
            return HealthResult(
                component="analysis_poller",
                status=status,
                message=f"Last run {age_str}, {unanalyzed} unanalyzed",
                key_metric=f"last: {age_str}",
                checks=checks,
                details={
                    "enabled": True,
                    "last_run_at": last_started.isoformat(),
                    "last_run_status": last_run["status"],
                    "last_run_error": last_run["error"],
                    "unanalyzed": unanalyzed,
                },
                raw={
                    "config_value": flag,
                    "unanalyzed_count": unanalyzed,
                    "last_run": {
                        "started_at": last_started.isoformat(),
                        "status": last_run["status"],
                        "error": last_run["error"],
                    },
                },
                last_activity=age_str,
            )
    except Exception as exc:
        logger.exception("Analysis poller health check failed")
        return HealthResult(
            component="analysis_poller",
            status=HealthStatus.RED,
            message=f"Check failed: {exc}",
            key_metric="error",
            checks=[ExpectedActual("Access", "queryable", str(exc), False)],
            raw={"error": str(exc)},
        )


# ---------------------------------------------------------------------------
# 8. Cross-boundary (local -> cloud ingest)
# ---------------------------------------------------------------------------
async def check_cross_boundary(pool) -> HealthResult:
    try:
        async with pool.acquire() as conn:
            # Count by event type
            rows = await conn.fetch(
                "SELECT event_type, COUNT(*) AS cnt, MAX(started_at) AS latest "
                "FROM orchestration_runs "
                "WHERE event_type IN ($1, $2, $3) "
                "AND started_at > NOW() - INTERVAL '24 hours' "
                "GROUP BY event_type",
                "ingest_status",
                "ingest_recommendation",
                "ingest_followup",
            )

            breakdown = {
                r["event_type"]: {
                    "count": r["cnt"],
                    "latest": r["latest"].isoformat() if r["latest"] else None,
                }
                for r in rows
            }
            total = sum(r["cnt"] for r in rows)
            latest = max((r["latest"] for r in rows if r["latest"]), default=None)
            latest_str = _ago(latest) if latest else "never"

            checks = [
                ExpectedActual(
                    "Recent ingests",
                    "ingest payloads in last 24h",
                    f"{total} payloads ({latest_str})",
                    total > 0,
                ),
            ]
            for evt_type in [
                "ingest_status",
                "ingest_recommendation",
                "ingest_followup",
            ]:
                info = breakdown.get(evt_type, {"count": 0, "latest": None})
                checks.append(
                    ExpectedActual(
                        evt_type.replace("ingest_", ""),
                        "receiving payloads",
                        f"{info['count']} in 24h",
                        True,
                    )
                )

            status = HealthStatus.GREEN if total > 0 else HealthStatus.YELLOW
            return HealthResult(
                component="cross_boundary",
                status=status,
                message=f"{total} payloads in 24h, last: {latest_str}",
                key_metric=f"{total} payloads today",
                checks=checks,
                details={
                    "total_24h": total,
                    "breakdown": breakdown,
                    "latest": latest_str,
                },
                raw={"orchestration_runs_by_type": breakdown},
            )
    except Exception as exc:
        logger.exception("Cross-boundary health check failed")
        return HealthResult(
            component="cross_boundary",
            status=HealthStatus.RED,
            message=f"Check failed: {exc}",
            key_metric="error",
            checks=[ExpectedActual("Access", "queryable", str(exc), False)],
            raw={"error": str(exc)},
        )


# ---------------------------------------------------------------------------
# Run all checks
# ---------------------------------------------------------------------------
async def run_all_checks(pool) -> dict[str, Any]:
    """Execute every health check concurrently and return an aggregate result."""

    async def _safe(coro, component_name: str) -> HealthResult:
        try:
            return await asyncio.wait_for(coro, timeout=_CHECK_TIMEOUT)
        except asyncio.TimeoutError:
            return HealthResult(
                component=component_name,
                status=HealthStatus.RED,
                message=f"Health check timed out after {_CHECK_TIMEOUT}s",
                key_metric="timeout",
                checks=[
                    ExpectedActual(
                        "Timeout",
                        f"complete within {_CHECK_TIMEOUT}s",
                        "timed out",
                        False,
                    )
                ],
            )
        except Exception as exc:
            logger.exception("Health check %s failed unexpectedly", component_name)
            return HealthResult(
                component=component_name,
                status=HealthStatus.RED,
                message=f"Unexpected error: {exc}",
                key_metric="error",
                checks=[ExpectedActual("Check", "no errors", str(exc), False)],
            )

    try:
        results: list[HealthResult] = await asyncio.gather(
            _safe(check_rds(pool), "rds"),
            _safe(check_s3(), "s3"),
            _safe(check_sqs(), "sqs"),
            _safe(
                check_lambda("job-search-platform-fetch", AWS_REGION), "lambda_fetch"
            ),
            _safe(
                check_lambda("job-search-platform-persist", AWS_REGION),
                "lambda_persist",
            ),
            _safe(check_eventbridge("job-search-platform", AWS_REGION), "eventbridge"),
            _safe(check_bedrock_kb(), "bedrock_kb"),
            _safe(check_analysis_poller(pool), "analysis_poller"),
            _safe(check_cross_boundary(pool), "cross_boundary"),
        )

        components = {r.component: r.to_dict() for r in results}
        overall = _worst([r.status for r in results])

        return {
            "components": components,
            "overall": overall.value,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        logger.exception("run_all_checks failed catastrophically")
        return {
            "components": {},
            "overall": HealthStatus.RED.value,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "error": str(exc),
        }


# ---------------------------------------------------------------------------
# Local mode: boto3 checks + cloud API proxy (no direct DB access)
# ---------------------------------------------------------------------------
async def run_all_checks_local() -> dict[str, Any]:
    """Run health checks locally: boto3 direct + cloud API proxy for RDS data."""

    from local.debug.cloud_proxy import (
        check_cross_boundary_via_api,
        check_orchestration_via_api,
        check_rds_via_api,
    )

    async def _safe(coro, component_name: str) -> HealthResult:
        try:
            return await asyncio.wait_for(coro, timeout=_CHECK_TIMEOUT)
        except asyncio.TimeoutError:
            return HealthResult(
                component=component_name,
                status=HealthStatus.RED,
                message=f"Timed out after {_CHECK_TIMEOUT}s",
                key_metric="timeout",
                checks=[
                    ExpectedActual(
                        "Timeout", f"< {_CHECK_TIMEOUT}s", "timed out", False
                    )
                ],
            )
        except Exception as exc:
            logger.exception("Health check %s failed", component_name)
            return HealthResult(
                component=component_name,
                status=HealthStatus.RED,
                message=f"Error: {exc}",
                key_metric="error",
                checks=[ExpectedActual("Check", "no errors", str(exc), False)],
            )

    try:
        results: list[HealthResult] = await asyncio.gather(
            # Direct boto3 checks (use local AWS credentials)
            # Lambda checks removed — JD ingestion now handled by ECS
            _safe(check_s3(), "s3"),
            _safe(check_sqs(), "sqs"),
            _safe(check_eventbridge("job-search-platform", AWS_REGION), "eventbridge"),
            _safe(check_bedrock_kb(), "bedrock_kb"),
            # Cloud API proxy checks (query existing cloud endpoints)
            _safe(check_rds_via_api(), "rds"),
            _safe(check_orchestration_via_api(), "analysis_poller"),
            _safe(check_cross_boundary_via_api(), "cross_boundary"),
        )

        components = {r.component: r.to_dict() for r in results}
        overall = _worst([r.status for r in results])

        return {
            "components": components,
            "overall": overall.value,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "mode": "local",
        }
    except Exception as exc:
        logger.exception("run_all_checks_local failed")
        return {
            "components": {},
            "overall": HealthStatus.RED.value,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "error": str(exc),
        }
