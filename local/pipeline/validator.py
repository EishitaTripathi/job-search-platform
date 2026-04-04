"""Validation Pipeline — format → Presidio → entity match.

All local → cloud data crosses through this module. Nothing else is allowed.
"""

import logging

from local.agents.shared.db import acquire
from local.agents.shared.redactor import PiiRedactor
from local.pipeline.allowlist import is_company_allowed, queue_unknown_company
from local.pipeline.schemas import (
    FollowupPayload,
    RecommendationPayload,
    StatusPayload,
)

logger = logging.getLogger(__name__)
_redactor = None


def _get_redactor():
    global _redactor
    if _redactor is None:
        _redactor = PiiRedactor()
    return _redactor


async def validate_status(payload: dict) -> StatusPayload:
    """Validate a status update payload."""
    # Stage 1: Format validation (Pydantic)
    validated = StatusPayload(**payload)

    # Stage 2: No string fields to PII-check (job_id is int, stage is enum, deadline is date)

    # Stage 3: Entity match — verify job_id exists
    async with acquire() as conn:
        exists = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM jobs WHERE id = $1)", validated.job_id
        )
        if not exists:
            raise ValueError(f"job_id {validated.job_id} not found")

    await _log_metric("status", "validated")
    return validated


async def validate_recommendation(payload: dict) -> RecommendationPayload:
    """Validate a recommendation payload."""
    # Stage 1: Format validation
    validated = RecommendationPayload(**payload)

    # Stage 2: Presidio PII check on string fields
    redactor = _get_redactor()
    for field_name in ["company", "role"]:
        value = getattr(validated, field_name)
        if redactor.contains_pii(value):
            await _log_metric("recommendation", "failed_pii")
            raise ValueError(f"PII detected in {field_name}: {value}")

    # Stage 3: Company allowlist check
    if not await is_company_allowed(validated.company):
        await queue_unknown_company(validated.company, validated.role)
        await _log_metric("recommendation", "queued_unknown")
        raise ValueError(f"Unknown company '{validated.company}' — queued for review")

    await _log_metric("recommendation", "validated")
    return validated


async def validate_followup(payload: dict) -> FollowupPayload:
    """Validate a follow-up payload."""
    # Stage 1: Format validation
    validated = FollowupPayload(**payload)

    # Stage 2: No string fields to PII-check

    # Stage 3: Entity match — verify job_id exists
    async with acquire() as conn:
        exists = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM jobs WHERE id = $1)", validated.job_id
        )
        if not exists:
            raise ValueError(f"job_id {validated.job_id} not found")

    await _log_metric("followup", "validated")
    return validated


async def _log_metric(payload_type: str, status: str):
    """Log validation result to pipeline_metrics."""
    try:
        async with acquire() as conn:
            await conn.execute(
                "INSERT INTO pipeline_metrics (source, metric_name, metric_value) VALUES ($1, $2, 1)",
                payload_type,
                status,
            )
    except Exception as e:
        logger.warning("Failed to log pipeline metric: %s", e)
