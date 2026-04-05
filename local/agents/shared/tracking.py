"""Tracking: MLflow experiment tracking + orchestration run lifecycle.

MLflow usage:
    with track_agent_run("email_classifier", {"email_id": "abc123"}) as run:
        run.log_metric("confidence", 0.85)
        run.log_param("stage", "status_update")

Orchestration run usage:
    run_id = await create_orchestration_run("email_check", "scheduler", ["email_classifier"])
    await update_orchestration_run(run_id, "completed", agent_results={...})
"""

import json
import logging
import os
import time
import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import mlflow

from local.agents.shared.db import acquire

logger = logging.getLogger(__name__)


def _ensure_tracking_uri():
    """Set MLflow tracking URI from environment."""
    uri = os.environ.get("MLFLOW_TRACKING_URI", "http://localhost:5001")
    mlflow.set_tracking_uri(uri)


@contextmanager
def track_agent_run(agent_name: str, params: dict | None = None):
    """Context manager that creates an MLflow run for an agent execution.

    Gracefully degrades if MLflow is unreachable — agents still run.
    """
    try:
        _ensure_tracking_uri()
        mlflow.set_experiment(agent_name)

        with mlflow.start_run():
            start = time.time()
            if params:
                mlflow.log_params(params)
            try:
                yield mlflow
            finally:
                elapsed = time.time() - start
                mlflow.log_metric("duration_seconds", elapsed)
    except Exception:
        logger.debug("MLflow unavailable, skipping tracking for %s", agent_name)
        yield SimpleNamespace(
            log_metric=lambda *a, **k: None, log_params=lambda *a, **k: None
        )


# ---------------------------------------------------------------------------
# Orchestration run lifecycle (moved from coordinator/tools.py)
# ---------------------------------------------------------------------------


async def create_orchestration_run(
    event_type: str,
    event_source: str,
    agent_chain: list[str],
) -> str:
    """Create a new orchestration run record. Returns the run_id."""
    run_id = str(uuid.uuid4())
    # Local DB — orchestration metadata stays on this machine
    async with acquire() as conn:
        await conn.execute(
            """
            INSERT INTO orchestration_runs (run_id, event_type, event_source, agent_chain, status)
            VALUES ($1, $2, $3, $4, 'running')
            """,
            uuid.UUID(run_id),
            event_type,
            event_source,
            agent_chain,
        )
    return run_id


async def update_orchestration_run(
    run_id: str,
    status: str,
    agent_results: dict | None = None,
    error: str | None = None,
) -> None:
    """Update an orchestration run with results or error."""
    error_sanitized = error[:500] if error else None
    # Local DB — error messages may contain email context, stays on this machine
    async with acquire() as conn:
        await conn.execute(
            """
            UPDATE orchestration_runs
            SET status = $1,
                agent_results = $2,
                error = $3,
                completed_at = CASE WHEN $1 IN ('completed', 'failed') THEN NOW() ELSE NULL END
            WHERE run_id = $4
            """,
            status,
            json.dumps(agent_results) if agent_results else None,
            error_sanitized,
            uuid.UUID(run_id),
        )
