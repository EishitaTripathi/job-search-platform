"""Tools for Recommendation Parser agent.

Extracts {company, role} pairs from recommendation emails using Phi-3,
validates each through the pipeline, and sends to cloud.
Does NOT send raw email content anywhere.
"""

import json
import logging
import re

from local.agents.shared.llm import llm_generate, sanitize_for_prompt
from local.pipeline.validator import validate_recommendation
from local.pipeline.sender import send_to_cloud

logger = logging.getLogger(__name__)

EXTRACTION_SYSTEM_PROMPT = (
    "You are an entity extractor for a job search platform. "
    "Given a job recommendation email, extract all company and role pairs. "
    "Respond with valid JSON only — a JSON array of objects."
)


async def extract_company_role_pairs(body: str) -> list[dict]:
    """Use Phi-3 to extract {company, role} pairs from email body.

    Returns: [{"company": "...", "role": "..."}, ...]
    """
    sanitized_body = sanitize_for_prompt(body[:4000])

    prompt = (
        f"Extract all job recommendations from this email:\n\n"
        f"{sanitized_body}\n\n"
        "Respond with ONLY a JSON array: "
        '[{"company": "Company Name", "role": "Role Title"}]\n'
        "If no recommendations found, return an empty array: []"
    )

    response = await llm_generate(prompt, system=EXTRACTION_SYSTEM_PROMPT)
    return _parse_extraction_response(response)


def _parse_extraction_response(response: str) -> list[dict]:
    """Parse LLM JSON response for company/role extraction."""
    # Try to extract JSON array from response
    array_match = re.search(r"\[.*\]", response, re.DOTALL)
    if array_match:
        try:
            result = json.loads(array_match.group())
            if isinstance(result, list):
                validated = []
                for item in result:
                    if (
                        isinstance(item, dict)
                        and "company" in item
                        and "role" in item
                        and isinstance(item["company"], str)
                        and isinstance(item["role"], str)
                    ):
                        validated.append(
                            {
                                "company": item["company"].strip(),
                                "role": item["role"].strip(),
                            }
                        )
                return validated
        except json.JSONDecodeError:
            pass

    return []


async def validate_and_send_recommendations(
    pairs: list[dict],
) -> int:
    """Validate each company/role pair and send through pipeline.

    Returns the number of successfully sent recommendations.
    """
    sent_count = 0
    for pair in pairs:
        try:
            validated = await validate_recommendation(pair)
            success = await send_to_cloud("recommendation", validated)
            if success:
                sent_count += 1
        except (ValueError, Exception) as e:
            logger.warning(
                "Recommendation validation failed for %s: %s",
                pair.get("company", "unknown"),
                e,
            )
    return sent_count
