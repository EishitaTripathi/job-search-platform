"""Resume Matcher tools — Sonnet + Titan v2 powered matching pipeline.

recall: KB retrieval (Titan v2 embeddings via Bedrock Knowledge Base)
structured_filter: SQL-based filtering on deal_breakers and experience
rerank: Sonnet-powered scoring of each candidate
store_reports: asyncpg INSERT into match_reports
"""

import json
import logging

from api.agents.bedrock_client import (
    SONNET,
    async_invoke_model,
    async_retrieve_from_kb,
    sanitize_for_prompt,
)

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


async def resolve_job_ids(candidates: list[dict], conn) -> list[dict]:
    """Map KB recall results back to job IDs via jd_s3_key lookup.

    Each candidate has an s3_uri like 's3://bucket/jds/hash.txt'.
    We strip the bucket prefix to get the jd_s3_key, then batch-lookup in jobs.
    Candidates that don't resolve to a job are dropped.
    """
    if not candidates:
        return []

    # Candidates with job_id already set (from targeted recall) skip resolution
    already_resolved = [c for c in candidates if c.get("job_id") is not None]
    needs_resolution = [c for c in candidates if c.get("job_id") is None]

    if not needs_resolution:
        return already_resolved

    # Extract jd_s3_key from each s3_uri
    s3_key_map = {}
    for c in needs_resolution:
        uri = c.get("s3_uri", "")
        # s3_uri format: s3://bucket-name/jds/hash.txt
        if uri.startswith("s3://"):
            parts = uri.split("/", 3)
            if len(parts) == 4:
                s3_key = parts[3]  # e.g. "jds/hash.txt"
                s3_key_map.setdefault(s3_key, []).append(c)

    if not s3_key_map and not already_resolved:
        logger.warning(
            "Resume Matcher: no valid s3_uri found in %d candidates",
            len(needs_resolution),
        )
        return []
    if not s3_key_map:
        return already_resolved

    # Batch lookup
    rows = await conn.fetch(
        "SELECT id, jd_s3_key FROM jobs WHERE jd_s3_key = ANY($1)",
        list(s3_key_map.keys()),
    )

    key_to_job_id = {row["jd_s3_key"]: row["id"] for row in rows}

    resolved = []
    for s3_key, cands in s3_key_map.items():
        job_id = key_to_job_id.get(s3_key)
        if job_id is not None:
            for c in cands:
                resolved.append({**c, "job_id": job_id})
        else:
            logger.debug(
                "Resume Matcher: no job found for s3_key=%s (orphaned KB doc)", s3_key
            )

    logger.info(
        "Resume Matcher: resolved %d/%d candidates to job IDs",
        len(resolved) + len(already_resolved),
        len(candidates),
    )
    return already_resolved + resolved


async def recall(resume_text: str, top_k: int = 50) -> list[dict]:
    """Retrieve candidate JDs from Bedrock Knowledge Base using resume as query.

    Uses Titan v2 embeddings under the hood via the KB's configured embedding model.
    Returns up to top_k results with content, score, and s3_uri.
    """
    # Build a focused query from the resume
    query = (
        f"Find job descriptions matching this candidate profile:\n{resume_text[:2000]}"
    )
    results = await async_retrieve_from_kb(query, top_k=top_k)
    logger.info("Resume Matcher recall: retrieved %d candidates", len(results))
    return results


async def structured_filter(
    candidates: list[dict], conn, resume_meta: dict | None = None
) -> list[dict]:
    """SQL-based filtering: remove candidates with deal_breakers or experience mismatches.

    Queries jd_analyses for each candidate's job to check deal_breakers and
    experience requirements against the resume's profile.

    Args:
        candidates: List of recall results (each has s3_uri, content, score).
        conn: asyncpg connection.
        resume_meta: Optional dict with experience_years, etc.

    Returns:
        Filtered list of candidates that pass structured checks.
    """
    if not candidates:
        return []

    # Get all jd_analyses to check deal_breakers
    rows = await conn.fetch(
        """
        SELECT ja.job_id, ja.deal_breakers, ja.experience_range,
               j.company, j.role
        FROM jd_analyses ja
        JOIN jobs j ON j.id = ja.job_id
        WHERE j.status NOT IN ('rejected', 'withdrawn')
        """
    )

    # Build lookup by job_id
    analyses_by_job = {}
    for row in rows:
        analyses_by_job[row["job_id"]] = dict(row)

    experience_years = (resume_meta or {}).get("experience_years")

    filtered = []
    for candidate in candidates:
        candidate_job_id = candidate.get("job_id")
        should_include = True

        if candidate_job_id is not None:
            analysis = analyses_by_job.get(candidate_job_id)
            if analysis:
                # Check deal_breakers
                deal_breakers = analysis.get("deal_breakers")
                if deal_breakers:
                    try:
                        breakers = (
                            json.loads(deal_breakers)
                            if isinstance(deal_breakers, str)
                            else deal_breakers
                        )
                    except (json.JSONDecodeError, TypeError):
                        breakers = []

                    if any("no_sponsorship" in str(b).lower() for b in breakers):
                        should_include = False
                        logger.info(
                            "Filtered out job_id=%d (%s): deal_breaker hit",
                            candidate_job_id,
                            analysis.get("company"),
                        )

                # Experience filter
                if should_include:
                    exp_range = analysis.get("experience_range")
                    if experience_years is not None and exp_range:
                        range_lower = (
                            exp_range.lower if hasattr(exp_range, "lower") else None
                        )
                        if range_lower is not None and experience_years < range_lower:
                            should_include = False
                            logger.info(
                                "Filtered out job_id=%d: requires %d+ years, candidate has %d",
                                candidate_job_id,
                                range_lower,
                                experience_years,
                            )

        if should_include:
            filtered.append(candidate)

    logger.info(
        "Resume Matcher filter: %d -> %d candidates after structured filter",
        len(candidates),
        len(filtered),
    )
    return filtered


async def rerank(candidates: list[dict], resume_text: str) -> list[dict]:
    """Use Sonnet to score and rank each candidate against the resume.

    Returns candidates with added score, category, gaps, and reasoning fields.
    """
    if not candidates:
        return []

    ranked = []
    for candidate in candidates:
        jd_content = candidate.get("content", "")
        if not jd_content:
            continue

        system = (
            "You are a resume-job matching expert. Score how well this candidate "
            "matches the job description. Return ONLY valid JSON:\n"
            "{\n"
            '  "overall_fit_score": 0.85,\n'
            '  "fit_category": "strong_match|good_match|partial_match|weak_match",\n'
            '  "gaps": ["gap1", "gap2"],\n'
            '  "strengths": ["strength1", "strength2"],\n'
            '  "reasoning": "one-paragraph explanation"\n'
            "}\n"
            "Score from 0.0 to 1.0. Be calibrated: 0.9+ is rare."
        )
        user_msg = (
            f"RESUME:\n{sanitize_for_prompt(resume_text[:3000])}\n\n"
            f"JOB DESCRIPTION:\n{sanitize_for_prompt(jd_content[:3000])}"
        )

        try:
            raw = await async_invoke_model(SONNET, system, user_msg, max_tokens=512)
            raw = raw.strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
                if raw.endswith("```"):
                    raw = raw[:-3]
                raw = raw.strip()

            match_result = json.loads(raw)
        except (json.JSONDecodeError, Exception) as exc:
            logger.error("Rerank failed for candidate: %s", exc)
            match_result = {
                "overall_fit_score": candidate.get("score", 0.0),
                "fit_category": "weak_match",
                "gaps": [],
                "strengths": [],
                "reasoning": "Rerank failed — using retrieval score as fallback",
            }

        ranked.append(
            {
                **candidate,
                "overall_fit_score": match_result.get("overall_fit_score", 0.0),
                "fit_category": match_result.get("fit_category", "weak_match"),
                "gaps": _ensure_list(match_result.get("gaps")),
                "strengths": _ensure_list(match_result.get("strengths")),
                "reasoning": match_result.get("reasoning", ""),
            }
        )

    # Sort by score descending
    ranked.sort(key=lambda x: x.get("overall_fit_score", 0.0), reverse=True)
    logger.info("Resume Matcher rerank: scored %d candidates", len(ranked))
    return ranked


async def store_reports(conn, resume_id: int, results: list[dict]) -> int:
    """Store match reports in database. Returns count of stored reports.

    Uses ON CONFLICT DO UPDATE to handle re-matching.
    """
    stored = 0
    for result in results:
        # Extract job_id from the result — requires the s3_uri or content to map back
        # In practice, the coordinator passes job_id through
        job_id = result.get("job_id")
        if not job_id:
            continue

        await conn.execute(
            """
            INSERT INTO match_reports (
                resume_id, job_id, overall_fit_score, fit_category,
                skill_gaps, reasoning
            )
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (resume_id, job_id) DO UPDATE SET
                overall_fit_score = EXCLUDED.overall_fit_score,
                fit_category = EXCLUDED.fit_category,
                skill_gaps = EXCLUDED.skill_gaps,
                reasoning = EXCLUDED.reasoning
            """,
            resume_id,
            job_id,
            result.get("overall_fit_score", 0.0),
            result.get("fit_category", "weak_match"),
            _ensure_list(result.get("gaps")),
            result.get("reasoning", ""),
        )
        stored += 1

    logger.info(
        "Resume Matcher: stored %d match reports for resume_id=%d", stored, resume_id
    )
    return stored
