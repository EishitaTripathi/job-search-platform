"""Tools for Deadline Tracker agent.

Extracts concrete deadlines from email bodies using Phi-3, and sends
each deadline as part of a StatusPayload through the validation pipeline.
"""

import json
import logging
import re
from datetime import date as date_type

from local.agents.shared.llm import llm_generate, sanitize_for_prompt
from local.pipeline.validator import validate_status
from local.pipeline.sender import send_to_cloud

logger = logging.getLogger(__name__)

DEADLINE_SYSTEM_PROMPT = (
    "You are a deadline extraction tool for a job search platform. "
    "Extract any concrete deadlines, due dates, or scheduled dates from "
    "the email. Respond with valid JSON only."
)


async def extract_deadlines(body: str) -> list[dict]:
    """Use Phi-3 to extract dates from email body.

    Returns: [{"date": "YYYY-MM-DD", "description": "..."}, ...]
    """
    sanitized_body = sanitize_for_prompt(body[:4000])

    prompt = (
        "Extract any concrete deadlines, due dates, or scheduled dates "
        "from this email. Return JSON: "
        '[{"date": "YYYY-MM-DD", "description": "..."}]\n\n'
        f"Email:\n{sanitized_body}\n\n"
        "If no deadlines found, return an empty array: []"
    )

    response = await llm_generate(prompt, system=DEADLINE_SYSTEM_PROMPT)
    return _parse_deadline_response(response)


def _parse_deadline_response(response: str) -> list[dict]:
    """Parse LLM JSON response for deadline extraction."""
    array_match = re.search(r"\[.*\]", response, re.DOTALL)
    if array_match:
        try:
            result = json.loads(array_match.group())
            if isinstance(result, list):
                validated = []
                for item in result:
                    if (
                        isinstance(item, dict)
                        and "date" in item
                        and isinstance(item["date"], str)
                    ):
                        # Validate date format
                        try:
                            date_type.fromisoformat(item["date"])
                            validated.append(
                                {
                                    "date": item["date"],
                                    "description": item.get("description", ""),
                                }
                            )
                        except ValueError:
                            continue
                return validated
        except json.JSONDecodeError:
            pass

    return []


async def send_deadlines_to_pipeline(
    job_id: int,
    stage: str,
    deadlines: list[dict],
) -> int:
    """Send each deadline as a StatusPayload through the pipeline.

    Returns the number of successfully sent payloads.
    """
    sent_count = 0
    for deadline in deadlines:
        payload = {
            "job_id": job_id,
            "stage": stage,
            "deadline": date_type.fromisoformat(deadline["date"]),
        }
        try:
            validated = await validate_status(payload)
            success = await send_to_cloud("status", validated)
            if success:
                sent_count += 1
        except (ValueError, Exception) as e:
            logger.warning(
                "Deadline pipeline validation failed for %s: %s",
                deadline.get("date", "unknown"),
                e,
            )
    return sent_count
