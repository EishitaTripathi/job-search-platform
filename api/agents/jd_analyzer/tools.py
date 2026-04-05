"""JD Analyzer tools — Haiku-powered JD parsing and field extraction.

Strips boilerplate, extracts structured fields, stores analysis in RDS.
All text sanitized before LLM invocation. DB access via asyncpg conn parameter.
"""

import json
import logging

import asyncpg

from api.agents.bedrock_client import HAIKU, async_invoke_model, sanitize_for_prompt

logger = logging.getLogger(__name__)


def _ensure_list(val) -> list:
    """Coerce a value to a list — handles LLM returning a JSON string instead of a list."""
    if val is None:
        return []
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
        return [val]
    return []


async def strip_boilerplate(text: str) -> str:
    """Use Haiku to strip benefits, legal, salary, and equal opportunity boilerplate."""
    if not text or not text.strip():
        return ""
    system = (
        "You are a job description cleaner. Remove all boilerplate sections from the "
        "job description: benefits, salary/compensation, equal opportunity statements, "
        "legal disclaimers, and company overview paragraphs that don't describe the role. "
        "Return ONLY the cleaned job description text focusing on role responsibilities, "
        "requirements, and qualifications. Do not add any commentary."
    )
    cleaned = await async_invoke_model(
        HAIKU, system, sanitize_for_prompt(text), max_tokens=2048
    )
    return cleaned


async def extract_fields(text: str) -> dict:
    """Use Haiku to extract structured fields from a cleaned JD.

    Returns dict with: required_skills, preferred_skills, tech_stack, role_type,
    experience_min, experience_max, deal_breakers, confidence_scores.
    """
    if not text or not text.strip():
        return {}
    system = (
        "You are a job description analyzer. Extract structured information from the "
        "job description and return ONLY valid JSON with these fields:\n"
        "{\n"
        '  "required_skills": ["skill1", "skill2"],\n'
        '  "preferred_skills": ["skill1", "skill2"],\n'
        '  "tech_stack": ["technology1", "technology2"],\n'
        '  "role_type": "backend|frontend|fullstack|ml|data|devops|other",\n'
        '  "experience_min": 0,\n'
        '  "experience_max": null,\n'
        '  "deal_breakers": ["clearance required", "on-site only"],\n'
        '  "confidence_scores": {\n'
        '    "required_skills": 0.9,\n'
        '    "role_type": 0.8\n'
        "  }\n"
        "}\n"
        "If a field cannot be determined, use null or empty list. "
        "experience_min/max are integers (years). Return ONLY the JSON."
    )
    raw = await async_invoke_model(
        HAIKU, system, sanitize_for_prompt(text), max_tokens=1024
    )

    # Parse JSON from response (handle markdown code fences)
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()

    try:
        fields = json.loads(raw)
    except json.JSONDecodeError:
        logger.error("Failed to parse JD fields JSON: %s", raw[:200])
        fields = {
            "required_skills": [],
            "preferred_skills": [],
            "tech_stack": [],
            "role_type": "other",
            "experience_min": None,
            "experience_max": None,
            "deal_breakers": [],
            "confidence_scores": {},
        }

    return fields


async def store_jd_analysis(
    conn, job_id: int, fields: dict, raw_jd_text: str = ""
) -> int:
    """Store JD analysis in database. Returns jd_analysis_id.

    Uses ON CONFLICT DO UPDATE to handle re-analysis of the same job.
    """
    row = await conn.fetchrow(
        """
        INSERT INTO jd_analyses (
            job_id, raw_jd_text, required_skills, preferred_skills, tech_stack,
            role_type, experience_range,
            deal_breakers, confidence_scores
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        ON CONFLICT (job_id) DO UPDATE SET
            raw_jd_text = EXCLUDED.raw_jd_text,
            required_skills = EXCLUDED.required_skills,
            preferred_skills = EXCLUDED.preferred_skills,
            tech_stack = EXCLUDED.tech_stack,
            role_type = EXCLUDED.role_type,
            experience_range = EXCLUDED.experience_range,
            deal_breakers = EXCLUDED.deal_breakers,
            confidence_scores = EXCLUDED.confidence_scores
        RETURNING id
        """,
        job_id,
        raw_jd_text,
        _ensure_list(fields.get("required_skills")),
        _ensure_list(fields.get("preferred_skills")),
        _ensure_list(fields.get("tech_stack")),
        fields.get("role_type", "other"),
        asyncpg.Range(
            fields.get("experience_min") or 0,
            fields.get("experience_max") or 100,
        ),
        _ensure_list(fields.get("deal_breakers")),
        json.dumps(fields.get("confidence_scores", {})),
    )
    return row["id"]
