"""Tools for Follow-up Advisor agent.

Simplified to daily_check mode only. Stage classification has been
extracted to Stage Classifier agent.

daily_check — queries stale jobs, generates follow-up recommendations,
and sends each through the validation pipeline.
"""

import logging
import re
from datetime import datetime, timezone

from local.agents.shared.llm import llm_generate, sanitize_for_prompt
from local.agents.shared.db import acquire
from local.pipeline.validator import validate_followup
from local.pipeline.sender import send_to_cloud

logger = logging.getLogger(__name__)

URGENCY_SYSTEM_PROMPT = (
    "You are a follow-up advisor for a job search platform. "
    "Given a job application's details and timeline, assess urgency and "
    "recommend a follow-up action. Be concise. "
    "Respond with valid JSON only."
)

# Map LLM free-text actions to pipeline-allowed action enums
_ACTION_MAP = {
    "send_followup": "send_followup",
    "follow up": "send_followup",
    "follow-up": "send_followup",
    "send follow": "send_followup",
    "check_status": "check_status",
    "check status": "check_status",
    "check application": "check_status",
    "withdraw": "withdraw",
    "move on": "withdraw",
}


def _normalize_action(raw_action: str) -> str:
    """Map free-text action to one of the pipeline-allowed enums."""
    lower = raw_action.lower().strip()
    for pattern, action in _ACTION_MAP.items():
        if pattern in lower:
            return action
    return "check_status"  # safe default


async def daily_check() -> list[dict]:
    """Query stale jobs and generate follow-up recommendations.

    Called by APScheduler at 9:05am daily.
    """
    async with acquire() as conn:
        stale_jobs = await conn.fetch(
            """
            SELECT j.id, j.company, j.role, j.status, j.date_posted, j.last_updated
            FROM jobs j
            WHERE j.status IN ('applied', 'assessment', 'assignment', 'interview')
              AND j.last_updated < NOW() - INTERVAL '7 days'
            ORDER BY j.last_updated ASC
            LIMIT 50
            """
        )

    recommendations = []
    for job in stale_jobs:
        days_stale = (datetime.now(timezone.utc) - job["last_updated"]).days

        if days_stale > 21:
            urgency = "high"
        elif days_stale > 14:
            urgency = "medium"
        else:
            urgency = "low"

        prompt = (
            f"Job: {sanitize_for_prompt(job['company'])} — {sanitize_for_prompt(job['role'])}\n"
            f"Status: {job['status']}\n"
            f"Days since last update: {days_stale}\n"
            f"Date posted: {job['date_posted']}\n\n"
            "What follow-up action should be taken?\n"
            'Respond with JSON: {"action": "brief recommendation", "reasoning": "why"}'
        )

        response = await llm_generate(prompt, system=URGENCY_SYSTEM_PROMPT)
        parsed = _parse_urgency_response(response)

        rec = {
            "job_id": job["id"],
            "urgency_level": urgency,
            "recommended_action": parsed.get("action", "Check application status"),
            "urgency_reasoning": parsed.get(
                "reasoning", f"{days_stale} days without update"
            ),
        }
        recommendations.append(rec)

        # Write to local database (PII allowed — stays on this machine)
        async with acquire() as conn:
            await conn.execute(
                """
                INSERT INTO followup_recommendations
                    (job_id, urgency_level, recommended_action, urgency_reasoning)
                VALUES ($1, $2, $3, $4)
                """,
                rec["job_id"],
                rec["urgency_level"],
                rec["recommended_action"],
                rec["urgency_reasoning"],
            )

    return recommendations


def _parse_urgency_response(response: str) -> dict:
    """Parse LLM JSON response for urgency assessment."""
    import json

    json_match = re.search(r"\{[^}]+\}", response, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group())
        except json.JSONDecodeError:
            pass
    return {}


async def send_followups_to_pipeline(recommendations: list[dict]) -> int:
    """Validate and send each recommendation as FollowupPayload through pipeline.

    Returns the number of successfully sent payloads.
    """
    sent_count = 0
    for rec in recommendations:
        action = _normalize_action(rec.get("recommended_action", "check_status"))
        payload = {
            "job_id": rec["job_id"],
            "urgency": rec["urgency_level"],
            "action": action,
        }
        try:
            validated = await validate_followup(payload)
            success = await send_to_cloud("followup", validated)
            if success:
                sent_count += 1
        except (ValueError, Exception) as e:
            logger.warning(
                "Followup pipeline validation failed for job %s: %s",
                rec.get("job_id", "unknown"),
                e,
            )
    return sent_count
