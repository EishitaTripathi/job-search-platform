"""Local pipeline health checks for the debug dashboard.

Checks Ollama, ChromaDB, local Postgres, Gmail API, ONNX embedder,
MLflow, scheduler (via orchestration runs), email pipeline, and labeling queue.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from api.debug.health_checks import (
    ExpectedActual,
    HealthResult,
    HealthStatus,
    _ago,
    _worst,
)

logger = logging.getLogger(__name__)

_CHECK_TIMEOUT = 10

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
CHROMADB_HOST = os.environ.get("CHROMADB_HOST", "localhost")
CHROMADB_PORT = int(os.environ.get("CHROMADB_PORT", "8000"))
DATABASE_URL = os.environ.get("DATABASE_URL", "")
MLFLOW_TRACKING_URI = os.environ.get("MLFLOW_TRACKING_URI", "http://localhost:5001")
ONNX_MODEL_PATH = os.environ.get("ONNX_MODEL_PATH", "local/models/all-MiniLM-L6-v2")
GMAIL_CREDENTIALS_PATH = os.environ.get(
    "GMAIL_CREDENTIALS_PATH", "credentials/credentials.json"
)
GMAIL_TOKEN_PATH = os.environ.get("GMAIL_TOKEN_PATH", "credentials/token.json")

_SCHEMA_PATH = Path(__file__).parent.parent.parent / "infra" / "schema.sql"
if not _SCHEMA_PATH.exists():
    _SCHEMA_PATH = Path("/app/infra/schema.sql")


# ---------------------------------------------------------------------------
# 1. Ollama
# ---------------------------------------------------------------------------
async def check_ollama() -> HealthResult:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            # Check if service is up and phi3:mini is loaded
            resp = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
            if resp.status_code != 200:
                return HealthResult(
                    component="ollama",
                    status=HealthStatus.RED,
                    message=f"API returned {resp.status_code}",
                    key_metric="unreachable",
                    checks=[
                        ExpectedActual(
                            "Service", "responds", f"HTTP {resp.status_code}", False
                        )
                    ],
                )

            models = resp.json().get("models", [])
            model_names = [m.get("name", "") for m in models]
            phi3_loaded = any("phi3" in n for n in model_names)

            checks = [
                ExpectedActual("Service", "Ollama API responds", "responds", True),
                ExpectedActual(
                    "Model",
                    "phi3:mini loaded",
                    f"loaded: {', '.join(model_names) or 'none'}",
                    phi3_loaded,
                ),
            ]

            if not phi3_loaded:
                return HealthResult(
                    component="ollama",
                    status=HealthStatus.YELLOW,
                    message=f"Service up but phi3:mini not loaded ({len(models)} models)",
                    key_metric="no phi3",
                    checks=checks,
                    raw={"models": model_names},
                )

            # Test inference
            start = time.time()
            try:
                inf_resp = await client.post(
                    f"{OLLAMA_BASE_URL}/api/chat",
                    json={
                        "model": "phi3:mini",
                        "messages": [{"role": "user", "content": "hi"}],
                        "stream": False,
                        "options": {"num_predict": 5},
                    },
                    timeout=15,
                )
                latency = round(time.time() - start, 2)
                if inf_resp.status_code == 200:
                    checks.append(
                        ExpectedActual(
                            "Inference",
                            "model responds",
                            f"responded in {latency}s",
                            True,
                        )
                    )
                    return HealthResult(
                        component="ollama",
                        status=HealthStatus.GREEN,
                        message=f"phi3:mini loaded, {latency}s latency",
                        key_metric=f"phi3 OK · {latency}s",
                        checks=checks,
                        raw={"models": model_names, "test_latency_s": latency},
                    )
                else:
                    checks.append(
                        ExpectedActual(
                            "Inference",
                            "model responds",
                            f"HTTP {inf_resp.status_code}",
                            False,
                        )
                    )
            except httpx.TimeoutException:
                latency = round(time.time() - start, 2)
                checks.append(
                    ExpectedActual(
                        "Inference",
                        "responds within 15s",
                        f"timed out after {latency}s",
                        False,
                    )
                )

            return HealthResult(
                component="ollama",
                status=HealthStatus.YELLOW,
                message="phi3:mini loaded but inference slow/failing",
                key_metric="phi3 slow",
                checks=checks,
                raw={"models": model_names},
            )
    except Exception as exc:
        logger.exception("Ollama check failed")
        return HealthResult(
            component="ollama",
            status=HealthStatus.RED,
            message=f"Unreachable: {exc}",
            key_metric="unreachable",
            checks=[ExpectedActual("Service", "responds", str(exc), False)],
        )


# ---------------------------------------------------------------------------
# 2. ChromaDB
# ---------------------------------------------------------------------------
async def check_chromadb() -> HealthResult:
    try:
        import chromadb

        def _check():
            client = chromadb.HttpClient(host=CHROMADB_HOST, port=CHROMADB_PORT)
            client.heartbeat()

            email_count = 0
            stage_count = 0
            try:
                email_coll = client.get_collection("email_classifications")
                email_count = email_coll.count()
            except Exception:
                pass
            try:
                stage_coll = client.get_collection("stage_classifications")
                stage_count = stage_coll.count()
            except Exception:
                pass

            return email_count, stage_count

        email_count, stage_count = await asyncio.to_thread(_check)

        checks = [
            ExpectedActual("Service", "ChromaDB responds", "connected", True),
            ExpectedActual(
                "Email collection",
                ">= 1 examples",
                f"{email_count} examples",
                email_count > 0,
            ),
            ExpectedActual(
                "Stage collection",
                ">= 1 examples",
                f"{stage_count} examples",
                stage_count > 0,
            ),
        ]

        if email_count == 0 and stage_count == 0:
            status = HealthStatus.YELLOW
            msg = "Connected but collections empty (cold start)"
        else:
            status = HealthStatus.GREEN
            msg = f"email: {email_count}, stage: {stage_count} examples"

        return HealthResult(
            component="chromadb",
            status=status,
            message=msg,
            key_metric=f"email: {email_count} · stage: {stage_count}",
            checks=checks,
            raw={
                "email_classifications": email_count,
                "stage_classifications": stage_count,
            },
        )
    except Exception as exc:
        logger.exception("ChromaDB check failed")
        return HealthResult(
            component="chromadb",
            status=HealthStatus.RED,
            message=f"Unreachable: {exc}",
            key_metric="unreachable",
            checks=[ExpectedActual("Service", "responds", str(exc), False)],
        )


# ---------------------------------------------------------------------------
# 3. Local Postgres
# ---------------------------------------------------------------------------
async def check_local_postgres() -> HealthResult:
    if not DATABASE_URL:
        return HealthResult(
            component="local_postgres",
            status=HealthStatus.RED,
            message="DATABASE_URL not configured",
            key_metric="not configured",
            checks=[ExpectedActual("Config", "DATABASE_URL set", "not set", False)],
        )

    try:
        import asyncpg

        conn = await asyncpg.connect(DATABASE_URL)
        try:
            version = await conn.fetchval("SELECT version()")

            # Row counts
            table_rows = await conn.fetch(
                "SELECT relname, n_live_tup::bigint AS row_count "
                "FROM pg_stat_user_tables ORDER BY n_live_tup DESC"
            )
            row_counts = {r["relname"]: r["row_count"] for r in table_rows}
            table_count = len(row_counts)

            # Schema sync
            schema_result = {
                "match": True,
                "missing_tables": [],
                "column_mismatches": {},
            }
            if _SCHEMA_PATH.exists():
                from api.debug.schema_sync import check_schema_match

                schema_result = await check_schema_match(conn, _SCHEMA_PATH)

            schema_ok = schema_result["match"]
            checks = [
                ExpectedActual("Connection", "active", "connected", True),
                ExpectedActual(
                    "Schema",
                    "13 tables matching schema.sql",
                    f"{table_count} tables"
                    + (
                        ""
                        if schema_ok
                        else f" (missing: {', '.join(schema_result['missing_tables'])})"
                    ),
                    schema_ok,
                ),
            ]

            status = HealthStatus.GREEN if schema_ok else HealthStatus.YELLOW
            return HealthResult(
                component="local_postgres",
                status=status,
                message=f"{table_count} tables"
                + (" · schema ✓" if schema_ok else " · schema mismatch"),
                key_metric=f"{table_count} tables · schema "
                + ("✓" if schema_ok else "✗"),
                checks=checks,
                details={"row_counts": row_counts, "schema": schema_result},
                raw={
                    "version": version,
                    "pg_stat_user_tables": row_counts,
                    "schema_diff": schema_result,
                },
            )
        finally:
            await conn.close()
    except Exception as exc:
        logger.exception("Local Postgres check failed")
        return HealthResult(
            component="local_postgres",
            status=HealthStatus.RED,
            message=f"Connection failed: {exc}",
            key_metric="disconnected",
            checks=[ExpectedActual("Connection", "active", str(exc), False)],
        )


# ---------------------------------------------------------------------------
# 4. Gmail API
# ---------------------------------------------------------------------------
async def check_gmail() -> HealthResult:
    try:
        creds_exist = os.path.exists(GMAIL_CREDENTIALS_PATH)
        token_exists = os.path.exists(GMAIL_TOKEN_PATH)

        checks = [
            ExpectedActual(
                "Credentials file",
                "exists",
                "found" if creds_exist else "missing",
                creds_exist,
            ),
            ExpectedActual(
                "Token file",
                "exists",
                "found" if token_exists else "missing",
                token_exists,
            ),
        ]

        if not creds_exist:
            return HealthResult(
                component="gmail",
                status=HealthStatus.RED,
                message="credentials.json missing",
                key_metric="no creds",
                checks=checks,
            )

        if not token_exists:
            return HealthResult(
                component="gmail",
                status=HealthStatus.YELLOW,
                message="token.json missing — needs OAuth flow",
                key_metric="no token",
                checks=checks,
            )

        # Check if token is valid
        try:
            import json

            with open(GMAIL_TOKEN_PATH) as f:
                token_data = json.load(f)
            has_refresh = bool(token_data.get("refresh_token"))
            expiry = token_data.get("expiry")

            checks.append(
                ExpectedActual(
                    "Refresh token",
                    "present",
                    "present" if has_refresh else "missing",
                    has_refresh,
                )
            )

            if expiry:
                try:
                    exp_dt = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
                    expired = exp_dt < datetime.now(timezone.utc)
                    checks.append(
                        ExpectedActual(
                            "Token expiry",
                            "not expired",
                            f"{'expired' if expired else 'valid'} (expires {_ago(exp_dt)})",
                            not expired or has_refresh,
                        )
                    )
                except Exception:
                    pass

            return HealthResult(
                component="gmail",
                status=HealthStatus.GREEN if has_refresh else HealthStatus.YELLOW,
                message="token valid"
                if has_refresh
                else "token present but no refresh token",
                key_metric="token valid" if has_refresh else "needs refresh",
                checks=checks,
                raw={"has_refresh_token": has_refresh, "expiry": expiry},
            )
        except Exception as exc:
            checks.append(ExpectedActual("Token parse", "valid JSON", str(exc), False))
            return HealthResult(
                component="gmail",
                status=HealthStatus.YELLOW,
                message=f"Token file exists but can't parse: {exc}",
                key_metric="token error",
                checks=checks,
            )
    except Exception as exc:
        logger.exception("Gmail check failed")
        return HealthResult(
            component="gmail",
            status=HealthStatus.RED,
            message=f"Check failed: {exc}",
            key_metric="error",
            checks=[ExpectedActual("Check", "no errors", str(exc), False)],
        )


# ---------------------------------------------------------------------------
# 5. ONNX Embedder
# ---------------------------------------------------------------------------
async def check_onnx() -> HealthResult:
    model_dir = Path(ONNX_MODEL_PATH)
    model_file = model_dir / "model.onnx"
    tokenizer_file = model_dir / "tokenizer.json"

    checks = [
        ExpectedActual(
            "Model file",
            "model.onnx exists",
            "found" if model_file.exists() else f"missing at {model_file}",
            model_file.exists(),
        ),
        ExpectedActual(
            "Tokenizer",
            "tokenizer.json exists",
            "found" if tokenizer_file.exists() else f"missing at {tokenizer_file}",
            tokenizer_file.exists(),
        ),
    ]

    if not model_file.exists() or not tokenizer_file.exists():
        return HealthResult(
            component="onnx_embedder",
            status=HealthStatus.RED,
            message="Model files missing",
            key_metric="missing",
            checks=checks,
            raw={"model_path": str(model_dir)},
        )

    try:

        def _test():
            from local.agents.shared.embedder import LocalEmbedder

            embedder = LocalEmbedder()
            vec = embedder.embed("test")
            return len(vec)

        dim = await asyncio.to_thread(_test)
        checks.append(
            ExpectedActual(
                "Embedding", "384-dim vector", f"{dim}-dim vector", dim == 384
            )
        )

        return HealthResult(
            component="onnx_embedder",
            status=HealthStatus.GREEN,
            message=f"Model loaded, {dim}-dim output",
            key_metric=f"{dim}-dim ✓",
            checks=checks,
            raw={"model_path": str(model_dir), "output_dim": dim},
        )
    except Exception as exc:
        logger.exception("ONNX embedder check failed")
        checks.append(ExpectedActual("Embedding", "produces vectors", str(exc), False))
        return HealthResult(
            component="onnx_embedder",
            status=HealthStatus.RED,
            message=f"Model broken: {exc}",
            key_metric="broken",
            checks=checks,
            raw={"error": str(exc)},
        )


# ---------------------------------------------------------------------------
# 6. MLflow
# ---------------------------------------------------------------------------
async def check_mlflow() -> HealthResult:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(
                f"{MLFLOW_TRACKING_URI}/api/2.0/mlflow/experiments/search?max_results=1"
            )

        if resp.status_code != 200:
            return HealthResult(
                component="mlflow",
                status=HealthStatus.RED,
                message=f"API returned {resp.status_code}",
                key_metric="unreachable",
                checks=[
                    ExpectedActual(
                        "Service", "responds", f"HTTP {resp.status_code}", False
                    )
                ],
            )

        data = resp.json()
        experiments = data.get("experiments", [])
        exp_names = [
            e.get("name", "") for e in experiments if e.get("name") != "Default"
        ]

        checks = [
            ExpectedActual("Service", "MLflow API responds", "connected", True),
            ExpectedActual(
                "Experiments",
                ">= 1 experiments",
                f"{len(exp_names)} experiments: {', '.join(exp_names[:5])}"
                if exp_names
                else "none",
                len(exp_names) > 0,
            ),
        ]

        status = HealthStatus.GREEN if exp_names else HealthStatus.YELLOW
        return HealthResult(
            component="mlflow",
            status=status,
            message=f"{len(exp_names)} experiments",
            key_metric=f"{len(exp_names)} experiments",
            checks=checks,
            raw={"experiments": exp_names},
        )
    except Exception as exc:
        logger.exception("MLflow check failed")
        return HealthResult(
            component="mlflow",
            status=HealthStatus.YELLOW,
            message=f"Unreachable (graceful degradation): {exc}",
            key_metric="unreachable",
            checks=[ExpectedActual("Service", "responds", str(exc), False)],
        )


# ---------------------------------------------------------------------------
# 7. Scheduler (inferred from orchestration runs)
# ---------------------------------------------------------------------------
async def check_scheduler() -> HealthResult:
    if not DATABASE_URL:
        return HealthResult(
            component="apscheduler",
            status=HealthStatus.RED,
            message="DATABASE_URL not configured",
            key_metric="no DB",
            checks=[ExpectedActual("Config", "DATABASE_URL set", "not set", False)],
        )

    try:
        import asyncpg

        conn = await asyncpg.connect(DATABASE_URL)
        try:
            now = datetime.now(timezone.utc)

            # Check for recent email_check (every 2h → expect within 2.5h)
            last_email = await conn.fetchrow(
                "SELECT started_at, status FROM orchestration_runs "
                "WHERE event_type = $1 ORDER BY started_at DESC LIMIT 1",
                "email_check",
            )
            # Check for recent daily_followup (daily → expect within 25h)
            last_followup = await conn.fetchrow(
                "SELECT started_at, status FROM orchestration_runs "
                "WHERE event_type = $1 ORDER BY started_at DESC LIMIT 1",
                "daily_followup",
            )

            checks = []
            email_ok = False
            if last_email:
                age = now - last_email["started_at"]
                email_ok = age < timedelta(hours=2, minutes=30)
                checks.append(
                    ExpectedActual(
                        "email_check",
                        "within last 2.5h",
                        f"{_ago(last_email['started_at'])} ({last_email['status']})",
                        email_ok,
                    )
                )
            else:
                checks.append(
                    ExpectedActual("email_check", "has run", "no runs found", False)
                )

            followup_ok = False
            if last_followup:
                age = now - last_followup["started_at"]
                followup_ok = age < timedelta(hours=25)
                checks.append(
                    ExpectedActual(
                        "daily_followup",
                        "within last 25h",
                        f"{_ago(last_followup['started_at'])} ({last_followup['status']})",
                        followup_ok,
                    )
                )
            else:
                checks.append(
                    ExpectedActual("daily_followup", "has run", "no runs found", False)
                )

            all_ok = email_ok and followup_ok
            any_ok = email_ok or followup_ok
            status = (
                HealthStatus.GREEN
                if all_ok
                else HealthStatus.YELLOW
                if any_ok
                else HealthStatus.RED
            )

            jobs_str = (
                "2 jobs" if all_ok else "1 job" if any_ok else "0 jobs"
            ) + " active"
            return HealthResult(
                component="apscheduler",
                status=status,
                message=jobs_str,
                key_metric=jobs_str,
                checks=checks,
            )
        finally:
            await conn.close()
    except Exception as exc:
        logger.exception("Scheduler check failed")
        return HealthResult(
            component="apscheduler",
            status=HealthStatus.RED,
            message=f"Check failed: {exc}",
            key_metric="error",
            checks=[ExpectedActual("Check", "queryable", str(exc), False)],
        )


# ---------------------------------------------------------------------------
# 8. Email pipeline
# ---------------------------------------------------------------------------
async def check_email_pipeline() -> HealthResult:
    if not DATABASE_URL:
        return HealthResult(
            component="email_classifier",
            status=HealthStatus.RED,
            message="DATABASE_URL not configured",
            key_metric="no DB",
            checks=[ExpectedActual("Config", "DATABASE_URL set", "not set", False)],
        )

    try:
        import asyncpg

        conn = await asyncpg.connect(DATABASE_URL)
        try:
            runs = await conn.fetch(
                "SELECT started_at, status, error, agent_results FROM orchestration_runs "
                "WHERE event_type = $1 ORDER BY started_at DESC LIMIT 10",
                "email_check",
            )

            if not runs:
                return HealthResult(
                    component="email_classifier",
                    status=HealthStatus.RED,
                    message="No email_check runs found",
                    key_metric="no runs",
                    checks=[
                        ExpectedActual(
                            "Runs", "email_check runs exist", "none found", False
                        )
                    ],
                )

            total = len(runs)
            completed = sum(1 for r in runs if r["status"] == "completed")
            failed = sum(1 for r in runs if r["status"] == "failed")
            last = runs[0]
            last_ago = _ago(last["started_at"])

            checks = [
                ExpectedActual(
                    "Recent runs",
                    "email_check runs exist",
                    f"{total} recent runs",
                    True,
                ),
                ExpectedActual(
                    "Success rate",
                    "mostly completed",
                    f"{completed} completed, {failed} failed",
                    failed == 0 or completed > failed,
                ),
            ]

            if last["error"]:
                checks.append(
                    ExpectedActual(
                        "Last error", "no errors", last["error"][:200], False
                    )
                )

            status = HealthStatus.GREEN if failed == 0 else HealthStatus.YELLOW
            return HealthResult(
                component="email_classifier",
                status=status,
                message=f"last: {last_ago}, {completed}/{total} ok",
                key_metric=f"last: {last_ago} · {failed} err",
                checks=checks,
                raw={
                    "last_run": {
                        "started_at": last["started_at"].isoformat(),
                        "status": last["status"],
                        "error": last["error"],
                    }
                },
            )
        finally:
            await conn.close()
    except Exception as exc:
        logger.exception("Email pipeline check failed")
        return HealthResult(
            component="email_classifier",
            status=HealthStatus.RED,
            message=f"Check failed: {exc}",
            key_metric="error",
            checks=[ExpectedActual("Check", "queryable", str(exc), False)],
        )


# ---------------------------------------------------------------------------
# 9. Labeling queue
# ---------------------------------------------------------------------------
async def check_labeling_queue() -> HealthResult:
    if not DATABASE_URL:
        return HealthResult(
            component="labeling_queue",
            status=HealthStatus.YELLOW,
            message="DATABASE_URL not configured",
            key_metric="no DB",
            checks=[ExpectedActual("Config", "DATABASE_URL set", "not set", False)],
        )

    try:
        import asyncpg

        conn = await asyncpg.connect(DATABASE_URL)
        try:
            pending = await conn.fetchval(
                "SELECT COUNT(*) FROM labeling_queue WHERE resolved = FALSE"
            )
            total = await conn.fetchval("SELECT COUNT(*) FROM labeling_queue")

            checks = [
                ExpectedActual(
                    "Pending reviews",
                    "manageable queue",
                    f"{pending} pending ({total} total)",
                    pending <= 10,
                ),
            ]

            status = HealthStatus.GREEN if pending <= 10 else HealthStatus.YELLOW
            return HealthResult(
                component="labeling_queue",
                status=status,
                message=f"{pending} pending review",
                key_metric=f"{pending} pending",
                checks=checks,
                raw={"pending": pending, "total": total},
            )
        finally:
            await conn.close()
    except Exception as exc:
        logger.exception("Labeling queue check failed")
        return HealthResult(
            component="labeling_queue",
            status=HealthStatus.YELLOW,
            message=f"Check failed: {exc}",
            key_metric="unknown",
            checks=[ExpectedActual("Check", "queryable", str(exc), False)],
        )


# ---------------------------------------------------------------------------
# Run all local checks
# ---------------------------------------------------------------------------
async def run_local_checks() -> dict[str, Any]:
    """Run all local pipeline health checks concurrently."""

    async def _safe(coro, name: str) -> HealthResult:
        try:
            return await asyncio.wait_for(coro, timeout=_CHECK_TIMEOUT)
        except asyncio.TimeoutError:
            return HealthResult(
                component=name,
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
            logger.exception("Local check %s failed", name)
            return HealthResult(
                component=name,
                status=HealthStatus.RED,
                message=f"Error: {exc}",
                key_metric="error",
                checks=[ExpectedActual("Check", "no errors", str(exc), False)],
            )

    results: list[HealthResult] = await asyncio.gather(
        _safe(check_ollama(), "ollama"),
        _safe(check_chromadb(), "chromadb"),
        _safe(check_local_postgres(), "local_postgres"),
        _safe(check_gmail(), "gmail"),
        _safe(check_onnx(), "onnx_embedder"),
        _safe(check_mlflow(), "mlflow"),
        _safe(check_scheduler(), "apscheduler"),
        _safe(check_email_pipeline(), "email_classifier"),
        _safe(check_labeling_queue(), "labeling_queue"),
    )

    components = {r.component: r.to_dict() for r in results}
    overall = _worst([r.status for r in results])

    return {
        "components": components,
        "overall": overall.value,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "mode": "local_pipeline",
    }
