"""Proxy RDS data through existing cloud API endpoints.

The debug dashboard runs locally and cannot reach RDS directly (private subnet).
Instead, it queries the cloud API's existing endpoints to get database-backed data:
- /api/runs → orchestration runs
- /api/ops/metrics → pipeline metrics
- /api/jobs → job counts
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx

from api.debug.health_checks import ExpectedActual, HealthResult, HealthStatus

logger = logging.getLogger(__name__)

CLOUD_API_URL = os.environ.get("CLOUD_API_URL", "")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")

_cached_token: str | None = None


async def _get_token() -> str:
    """Login to cloud API and return JWT token. Caches across calls."""
    global _cached_token
    if _cached_token:
        return _cached_token

    if not CLOUD_API_URL or not APP_PASSWORD:
        raise ValueError("CLOUD_API_URL and APP_PASSWORD must be set")

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{CLOUD_API_URL}/login",
            json={"password": APP_PASSWORD},
        )
        if resp.status_code != 200:
            raise ValueError(f"Login failed: {resp.status_code}")
        _cached_token = resp.cookies.get("token", "")
        if not _cached_token:
            raise ValueError("No token cookie in login response")
        return _cached_token


def clear_token_cache():
    """Clear cached JWT (e.g., on auth failure)."""
    global _cached_token
    _cached_token = None


async def _api_get(path: str, params: dict | None = None) -> Any:
    """GET a cloud API endpoint with JWT auth. Retries once on 401."""
    token = await _get_token()
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{CLOUD_API_URL}{path}",
            params=params,
            cookies={"token": token},
        )
        if resp.status_code == 401:
            clear_token_cache()
            token = await _get_token()
            resp = await client.get(
                f"{CLOUD_API_URL}{path}",
                params=params,
                cookies={"token": token},
            )
        if resp.status_code != 200:
            raise ValueError(f"API {path} returned {resp.status_code}")
        return resp.json()


# ---------------------------------------------------------------------------
# Cloud-proxied health checks
# ---------------------------------------------------------------------------


async def check_rds_via_api() -> HealthResult:
    """Check RDS health by querying cloud API for job/analysis counts."""
    if not CLOUD_API_URL:
        return HealthResult(
            component="rds",
            status=HealthStatus.RED,
            message="CLOUD_API_URL not configured",
            key_metric="not configured",
            checks=[
                ExpectedActual("Cloud API", "CLOUD_API_URL env set", "not set", False)
            ],
        )

    try:
        # Get job counts
        jobs = await _api_get("/api/jobs", {"sort": "date"})
        job_count = len(jobs) if isinstance(jobs, list) else 0

        # Get metrics
        metrics = await _api_get("/api/ops/metrics")
        metric_count = len(metrics) if isinstance(metrics, list) else 0

        checks = [
            ExpectedActual("Cloud API reachable", "API responds", "connected", True),
            ExpectedActual(
                "Jobs in database", "> 0 jobs", f"{job_count} jobs", job_count > 0
            ),
            ExpectedActual(
                "Pipeline metrics",
                "metrics recorded",
                f"{metric_count} metric groups",
                True,
            ),
        ]

        status = HealthStatus.GREEN if job_count > 0 else HealthStatus.YELLOW
        return HealthResult(
            component="rds",
            status=status,
            message=f"{job_count} jobs (via cloud API proxy)",
            key_metric=f"{job_count} jobs · via API",
            checks=checks,
            details={
                "job_count": job_count,
                "metric_groups": metric_count,
                "note": "Data via cloud API proxy — no direct RDS access",
            },
            raw={"jobs_sample_count": job_count, "metrics_groups": metric_count},
        )
    except Exception as exc:
        logger.exception("RDS proxy check failed")
        return HealthResult(
            component="rds",
            status=HealthStatus.RED,
            message=f"Cloud API unreachable: {exc}",
            key_metric="API unreachable",
            checks=[
                ExpectedActual("Cloud API reachable", "API responds", str(exc), False)
            ],
            raw={"error": str(exc)},
        )


async def check_orchestration_via_api() -> HealthResult:
    """Check orchestration runs via cloud API."""
    if not CLOUD_API_URL:
        return HealthResult(
            component="analysis_poller",
            status=HealthStatus.RED,
            message="CLOUD_API_URL not configured",
            key_metric="not configured",
            checks=[ExpectedActual("Cloud API", "configured", "not set", False)],
        )

    try:
        runs = await _api_get("/api/runs", {"limit": 10})
        if not isinstance(runs, list):
            runs = []

        total = len(runs)
        completed = sum(1 for r in runs if r.get("status") == "completed")
        failed = sum(1 for r in runs if r.get("status") == "failed")

        last_run_at = None
        if runs:
            last_run_at = runs[0].get("started_at")

        def _ago(ts):
            if not ts:
                return "never"
            try:
                dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                diff = datetime.now(timezone.utc) - dt
                mins = int(diff.total_seconds() / 60)
                if mins < 60:
                    return f"{mins}m ago"
                return f"{mins // 60}h ago"
            except Exception:
                return str(ts)

        last_str = _ago(last_run_at)

        checks = [
            ExpectedActual(
                "Recent runs",
                "orchestration runs exist",
                f"{total} recent runs",
                total > 0,
            ),
            ExpectedActual(
                "Success rate",
                "mostly completed",
                f"{completed} completed, {failed} failed",
                failed == 0 or completed > failed,
            ),
        ]

        status = HealthStatus.GREEN
        if total == 0:
            status = HealthStatus.YELLOW
        elif failed > completed:
            status = HealthStatus.YELLOW

        return HealthResult(
            component="analysis_poller",
            status=status,
            message=f"Last run: {last_str}, {completed}/{total} completed",
            key_metric=f"last: {last_str}",
            checks=checks,
            details={"recent_runs": runs[:5], "last_run_at": last_run_at},
            raw={"api_runs": runs[:5]},
            last_activity=last_str,
        )
    except Exception as exc:
        logger.exception("Orchestration proxy check failed")
        return HealthResult(
            component="analysis_poller",
            status=HealthStatus.RED,
            message=f"Cloud API unreachable: {exc}",
            key_metric="API unreachable",
            checks=[ExpectedActual("Cloud API", "reachable", str(exc), False)],
            raw={"error": str(exc)},
        )


async def check_cross_boundary_via_api() -> HealthResult:
    """Check local→cloud ingest activity via orchestration runs."""
    if not CLOUD_API_URL:
        return HealthResult(
            component="cross_boundary",
            status=HealthStatus.RED,
            message="CLOUD_API_URL not configured",
            key_metric="not configured",
            checks=[ExpectedActual("Cloud API", "configured", "not set", False)],
        )

    try:
        runs = await _api_get("/api/runs", {"limit": 50})
        if not isinstance(runs, list):
            runs = []

        ingest_types = {"ingest_status", "ingest_recommendation", "ingest_followup"}
        ingest_runs = [r for r in runs if r.get("event_type") in ingest_types]

        total = len(ingest_runs)
        breakdown = {}
        for r in ingest_runs:
            et = r.get("event_type", "unknown")
            breakdown[et] = breakdown.get(et, 0) + 1

        last_ingest = ingest_runs[0].get("started_at") if ingest_runs else None

        checks = [
            ExpectedActual(
                "Ingest activity",
                "local pipeline sending payloads",
                f"{total} ingest events in recent runs",
                total > 0,
            ),
        ]
        for et, count in breakdown.items():
            checks.append(
                ExpectedActual(
                    et.replace("ingest_", ""), "receiving", f"{count} events", True
                )
            )

        status = HealthStatus.GREEN if total > 0 else HealthStatus.YELLOW
        return HealthResult(
            component="cross_boundary",
            status=status,
            message=f"{total} ingest events in recent runs",
            key_metric=f"{total} payloads",
            checks=checks,
            details={
                "total": total,
                "breakdown": breakdown,
                "last_ingest": last_ingest,
            },
            raw={"ingest_runs": ingest_runs[:5]},
        )
    except Exception as exc:
        logger.exception("Cross-boundary proxy check failed")
        return HealthResult(
            component="cross_boundary",
            status=HealthStatus.RED,
            message=f"Cloud API unreachable: {exc}",
            key_metric="API unreachable",
            checks=[ExpectedActual("Cloud API", "reachable", str(exc), False)],
            raw={"error": str(exc)},
        )


async def fetch_summary() -> dict[str, Any]:
    """Fetch summary data from cloud API."""
    result = {
        "jobs": 0,
        "jd_analyses": 0,
        "match_reports": 0,
        "resumes": 0,
        "last_ingest": None,
        "last_analysis": None,
    }

    if not CLOUD_API_URL:
        return result

    try:
        jobs = await _api_get("/api/jobs", {"sort": "date"})
        result["jobs"] = len(jobs) if isinstance(jobs, list) else 0

        runs = await _api_get("/api/runs", {"limit": 50})
        if isinstance(runs, list):
            for r in runs:
                et = r.get("event_type", "")
                ts = r.get("started_at")
                if et.startswith("ingest_") and (
                    not result["last_ingest"] or ts > result["last_ingest"]
                ):
                    result["last_ingest"] = ts
                if et == "new_jd" and (
                    not result["last_analysis"] or ts > result["last_analysis"]
                ):
                    result["last_analysis"] = ts
    except Exception:
        logger.exception("Failed to fetch summary from cloud API")

    return result


async def fetch_component_runs(component_id: str) -> list[dict]:
    """Fetch recent orchestration runs for a component from cloud API."""
    if not CLOUD_API_URL:
        return []

    try:
        runs = await _api_get("/api/runs", {"limit": 20})
        if not isinstance(runs, list):
            return []

        # Filter runs that involve this component
        relevant = []
        for r in runs:
            chain = r.get("agent_chain", []) or []
            if component_id in chain or r.get("event_type") == component_id:
                relevant.append(r)
            if len(relevant) >= 5:
                break
        return relevant
    except Exception:
        logger.exception("Failed to fetch component runs for %s", component_id)
        return []
