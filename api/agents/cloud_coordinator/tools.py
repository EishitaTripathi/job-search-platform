"""Cloud Coordinator tools — orchestration run tracking and agent dispatch.

Manages orchestration_runs table and dispatches to downstream agents.
"""

import json
import logging
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


async def create_run(conn, event_type: str, event_data: dict | None = None) -> str:
    """Create an orchestration run record. Returns run_id (UUID string)."""
    run_id = str(uuid.uuid4())
    await conn.execute(
        """
        INSERT INTO orchestration_runs (run_id, event_type, event_source, status, started_at)
        VALUES ($1, $2, $3, 'running', $4)
        """,
        run_id,
        event_type,
        json.dumps(event_data or {}),
        datetime.now(timezone.utc),
    )
    logger.info(
        "Cloud Coordinator: created run %s for event_type=%s", run_id, event_type
    )
    return run_id


async def update_run(
    conn, run_id: str, status: str, results: dict | None = None
) -> None:
    """Update an orchestration run with status and results."""
    await conn.execute(
        """
        UPDATE orchestration_runs
        SET status = $2, agent_results = $3, completed_at = $4
        WHERE run_id = $1
        """,
        run_id,
        status,
        json.dumps(results or {}),
        datetime.now(timezone.utc),
    )
    logger.info("Cloud Coordinator: updated run %s status=%s", run_id, status)
