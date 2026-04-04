"""Sponsorship Screener tools — Haiku-powered sponsorship signal detection.

Default assumption: sponsorship is available. Only flags if JD explicitly
states "US citizen only", "no sponsorship", "security clearance required", etc.

Key nuance: "must be authorized to work in the US" does NOT mean no sponsorship.
Sponsorship IS a form of work authorization.
"""

import json
import logging

from api.agents.bedrock_client import HAIKU, async_invoke_model, sanitize_for_prompt

logger = logging.getLogger(__name__)

SPONSORSHIP_SYSTEM_PROMPT = """You are a sponsorship eligibility analyzer for job postings.

Your job is to determine whether a job posting explicitly EXCLUDES candidates who need visa sponsorship.

CRITICAL NUANCES:
- "Must be authorized to work in the United States" does NOT mean no sponsorship.
  Sponsorship (H-1B, etc.) IS a path to work authorization. This phrase alone is NEUTRAL.
- "Will not sponsor" or "no sponsorship available" = EXPLICIT exclusion.
- "US citizen or permanent resident required" = EXPLICIT exclusion.
- "Security clearance required" = LIKELY exclusion (clearances typically require citizenship).
- "Must be a US person" (ITAR/EAR context) = EXPLICIT exclusion.
- If the JD says nothing about sponsorship or work authorization = ASSUME AVAILABLE.

Return ONLY valid JSON:
{
  "sponsorship_status": "available|unavailable|unclear",
  "reasoning": "one-sentence explanation",
  "signals": ["exact quote from JD that informed decision"]
}

Default to "available" unless there is explicit evidence otherwise."""


async def analyze_sponsorship(jd_text: str) -> dict:
    """Analyze JD text for sponsorship exclusion signals."""
    raw = await async_invoke_model(
        HAIKU,
        SPONSORSHIP_SYSTEM_PROMPT,
        sanitize_for_prompt(jd_text),
        max_tokens=512,
    )

    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        logger.error("Failed to parse sponsorship JSON: %s", raw[:200])
        result = {
            "sponsorship_status": "available",
            "reasoning": "Parse error — defaulting to available",
            "signals": [],
        }

    # Enforce default assumption
    if result.get("sponsorship_status") not in ("available", "unavailable", "unclear"):
        result["sponsorship_status"] = "available"

    return result


async def update_deal_breakers(conn, job_id: int, sponsorship_result: dict) -> None:
    """If sponsorship is unavailable, add to deal_breakers in jd_analyses.

    We don't have a separate sponsorship column — instead we append to the
    existing deal_breakers JSONB array in jd_analyses.
    """
    if sponsorship_result.get("sponsorship_status") != "unavailable":
        return

    reasoning = sponsorship_result.get("reasoning", "No sponsorship available")
    deal_breaker_entry = f"no_sponsorship: {reasoning}"

    # Append to existing deal_breakers TEXT[] array
    await conn.execute(
        """
        UPDATE jd_analyses
        SET deal_breakers = array_cat(COALESCE(deal_breakers, '{}'::text[]), $2::text[])
        WHERE job_id = $1
        """,
        job_id,
        [deal_breaker_entry],
    )
    logger.info(
        "Sponsorship Screener: flagged job_id=%d as no-sponsorship: %s",
        job_id,
        reasoning,
    )
