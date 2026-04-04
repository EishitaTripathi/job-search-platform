"""Tools for Stage Classifier agent.

Extracted from followup_advisor/tools.py. Classifies status_update emails
into 8 application stages using RAG few-shot + Phi-3, then sends
StatusPayload through the validation pipeline.
"""

import json
import logging
import re

from local.agents.shared.embedder import LocalEmbedder
from local.agents.shared.llm import llm_generate, sanitize_for_prompt
from local.agents.shared.memory import get_stage_collection
from local.agents.shared.db import acquire
from local.agents.shared.redactor import enforce_pii_boundary
from local.pipeline.validator import validate_status
from local.pipeline.sender import send_to_cloud

logger = logging.getLogger(__name__)

VALID_STAGES = {
    "to_apply",
    "waiting_for_referral",
    "applied",
    "assessment",
    "assignment",
    "interview",
    "offer",
    "rejected",
}

_embedder: LocalEmbedder | None = None


def _get_embedder() -> LocalEmbedder:
    global _embedder
    if _embedder is None:
        _embedder = LocalEmbedder()
    return _embedder


# ============================================================================
# RAG Few-Shot Retrieval
# ============================================================================


async def retrieve_stage_examples(email_text: str, n_results: int = 5) -> list[dict]:
    """Retrieve similar stage-labeled emails from ChromaDB."""
    collection = get_stage_collection()
    if collection.count() == 0:
        return []

    embedding = _get_embedder().embed(email_text)
    results = collection.query(
        query_embeddings=[embedding],
        n_results=min(n_results, collection.count()),
    )

    examples = []
    for i in range(len(results["ids"][0])):
        examples.append(
            {
                "text": results["documents"][0][i],
                "stage": results["metadatas"][0][i]["stage"],
            }
        )
    return examples


# ============================================================================
# Stage Classification
# ============================================================================

STAGE_SYSTEM_PROMPT = (
    "You are an application stage classifier for a job search platform. "
    "Given a status update email about a job application, classify it into "
    "exactly one stage:\n"
    "- to_apply: job identified but not yet applied\n"
    "- waiting_for_referral: waiting for a referral before applying\n"
    "- applied: confirmation that application was received\n"
    "- assessment: online assessment or coding challenge\n"
    "- assignment: take-home assignment or project\n"
    "- interview: interview scheduled or invite\n"
    "- offer: job offer extended\n"
    "- rejected: application rejected or position filled\n\n"
    "Respond with valid JSON only."
)


async def classify_stage(email_text: str) -> dict:
    """Classify application stage using RAG few-shot + Phi-3.

    Returns: {"stage": str, "confidence": float}
    """
    examples = await retrieve_stage_examples(email_text)

    prompt_parts = []
    if examples:
        prompt_parts.append("Here are examples of previously classified emails:\n")
        for i, ex in enumerate(examples, 1):
            prompt_parts.append(f"Example {i}:")
            prompt_parts.append(f"Email: {sanitize_for_prompt(ex['text'][:500])}")
            prompt_parts.append(f"Stage: {ex['stage']}\n")

    prompt_parts.append("Now classify this email:\n")
    prompt_parts.append(f"Email: {sanitize_for_prompt(email_text[:2000])}")
    prompt_parts.append(
        "\nRespond with ONLY a JSON object: "
        '{"stage": "to_apply|waiting_for_referral|applied|assessment|'
        'assignment|interview|offer|rejected", '
        '"confidence": 0.0-1.0}'
    )

    response = await llm_generate("\n".join(prompt_parts), system=STAGE_SYSTEM_PROMPT)
    return _parse_stage_response(response)


def _parse_stage_response(response: str) -> dict:
    """Parse LLM JSON response for stage classification."""
    json_match = re.search(r"\{[^}]+\}", response, re.DOTALL)
    if json_match:
        try:
            result = json.loads(json_match.group())
            if result.get("stage") not in VALID_STAGES:
                result["stage"] = "applied"
                result["confidence"] = 0.0
            result.setdefault("confidence", 0.0)
            return result
        except json.JSONDecodeError:
            pass

    return {"stage": "applied", "confidence": 0.0}


# ============================================================================
# Storage & Queuing
# ============================================================================


async def store_stage_example(
    email_id: str,
    subject: str,
    snippet: str,
    stage: str,
    confirmed_by: str = "user",
) -> None:
    """Store confirmed stage classification in ChromaDB + PostgreSQL."""
    import struct

    email_text = f"{subject} {snippet}"
    embedding = _get_embedder().embed(email_text)

    collection = get_stage_collection()
    collection.upsert(
        ids=[email_id],
        documents=[email_text],
        embeddings=[embedding],
        metadatas=[{"stage": stage, "confirmed_by": confirmed_by}],
    )

    enforce_pii_boundary({"subject": subject, "snippet": snippet})
    embedding_bytes = bytes(struct.pack(f"{len(embedding)}f", *embedding))
    async with acquire() as conn:
        await conn.execute(
            """
            INSERT INTO labeled_emails (email_id, subject, snippet, embedding, stage, confirmed_by)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (email_id) DO UPDATE SET stage = $5, confirmed_by = $6
            """,
            email_id,
            subject,
            snippet,
            embedding_bytes,
            stage,
            confirmed_by,
        )


async def enqueue_stage_review(email_id: str, stage: str) -> None:
    """Update labeling queue with the guessed stage for human review."""
    enforce_pii_boundary({"stage": stage})
    async with acquire() as conn:
        await conn.execute(
            """
            UPDATE labeling_queue
            SET guessed_stage = $1
            WHERE email_id = $2
            """,
            stage,
            email_id,
        )


# ============================================================================
# Job ID Lookup & Pipeline Send
# ============================================================================


async def lookup_job_id(company: str | None, role: str | None) -> int | None:
    """Fuzzy match company+role against jobs table to find job_id."""
    if not company or not role:
        return None

    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id FROM jobs
            WHERE company ILIKE $1 AND role ILIKE $2
              AND status NOT IN ('rejected', 'offer')
            ORDER BY last_updated DESC
            LIMIT 1
            """,
            f"%{company}%",
            f"%{role}%",
        )
    return row["id"] if row else None


async def send_status_to_pipeline(
    job_id: int,
    stage: str,
    deadline: str | None = None,
) -> bool:
    """Validate and send StatusPayload through the pipeline to cloud."""
    from datetime import date as date_type

    payload = {
        "job_id": job_id,
        "stage": stage,
    }
    if deadline:
        payload["deadline"] = date_type.fromisoformat(deadline)

    try:
        validated = await validate_status(payload)
        return await send_to_cloud("status", validated)
    except (ValueError, Exception) as e:
        logger.warning("Status pipeline validation failed: %s", e)
        return False
