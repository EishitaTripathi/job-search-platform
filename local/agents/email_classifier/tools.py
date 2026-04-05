"""Tools for Email Classifier agent.

Three-way classification: irrelevant / status_update / recommendation.
RAG few-shot from ChromaDB — retrieve similar labeled emails, inject as examples.
Entity extraction: company, role, URLs from email content.
"""

import re

from local.agents.shared.embedder import LocalEmbedder
from local.agents.shared.llm import llm_generate, sanitize_for_prompt
from local.agents.shared.memory import get_email_collection
from local.agents.shared.db import acquire

VALID_LABELS = {"irrelevant", "status_update", "recommendation"}

_embedder: LocalEmbedder | None = None


def _get_embedder() -> LocalEmbedder:
    global _embedder
    if _embedder is None:
        _embedder = LocalEmbedder()
    return _embedder


async def retrieve_few_shot_examples(email_text: str, n_results: int = 5) -> list[dict]:
    """Retrieve similar labeled emails from ChromaDB for few-shot context."""
    collection = get_email_collection()
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
                "label": results["metadatas"][0][i]["label"],
            }
        )
    return examples


def _format_few_shot_prompt(examples: list[dict], email_text: str) -> str:
    """Build the classification prompt with few-shot examples."""
    prompt_parts = []

    if examples:
        prompt_parts.append("Here are examples of previously classified emails:\n")
        for i, ex in enumerate(examples, 1):
            prompt_parts.append(f"Example {i}:")
            prompt_parts.append(f"Email: {sanitize_for_prompt(ex['text'][:500])}")
            prompt_parts.append(f"Label: {ex['label']}\n")

    prompt_parts.append("Now classify this email:\n")
    prompt_parts.append(f"Email: {sanitize_for_prompt(email_text[:2000])}")
    prompt_parts.append(
        "\nRespond with ONLY a JSON object: "
        '{"label": "irrelevant|status_update|recommendation", '
        '"company": "extracted company or null", '
        '"role": "extracted role or null", '
        '"urls": ["any job URLs found"], '
        '"confidence": 0.0-1.0}'
    )
    return "\n".join(prompt_parts)


SYSTEM_PROMPT = (
    "You are an email classifier for a job search platform. "
    "Classify each email into exactly one of three categories:\n"
    "- irrelevant: not related to job search (newsletters, promotions, personal)\n"
    "- status_update: updates about a job application (confirmation, rejection, "
    "interview invite, assessment, assignment)\n"
    "- recommendation: contains job recommendations or listings with URLs\n\n"
    "Extract the company name, role title, and any job URLs if present. "
    "Respond with valid JSON only."
)


async def classify_email(email_text: str) -> dict:
    """Classify an email using RAG few-shot + Phi-3.

    Returns: {"label": str, "company": str|None, "role": str|None,
              "urls": list[str], "confidence": float}
    """
    examples = await retrieve_few_shot_examples(email_text)
    prompt = _format_few_shot_prompt(examples, email_text)
    response = await llm_generate(prompt, system=SYSTEM_PROMPT)
    return _parse_classification_response(response)


def _parse_classification_response(response: str) -> dict:
    """Parse LLM JSON response, with fallback for malformed output."""
    import json

    # Try to extract JSON from response
    json_match = re.search(r"\{[^}]+\}", response, re.DOTALL)
    if json_match:
        try:
            result = json.loads(json_match.group())
            # Validate label
            if result.get("label") not in VALID_LABELS:
                result["label"] = "irrelevant"
                result["confidence"] = 0.0
            # Ensure all fields exist
            result.setdefault("company", None)
            result.setdefault("role", None)
            result.setdefault("urls", [])
            result.setdefault("confidence", 0.0)
            return result
        except json.JSONDecodeError:
            pass

    # Fallback: couldn't parse — send to queue
    return {
        "label": "irrelevant",
        "company": None,
        "role": None,
        "urls": [],
        "confidence": 0.0,
    }


async def store_labeled_example(
    email_id: str,
    subject: str,
    snippet: str,
    label: str,
    confirmed_by: str = "user",
) -> None:
    """Store a confirmed classification in both PostgreSQL and ChromaDB.

    Called when user resolves a queue item or when auto-classification
    confidence exceeds threshold.
    """
    email_text = f"{subject} {snippet}"
    embedding = _get_embedder().embed(email_text)

    # Store in ChromaDB for future few-shot retrieval
    collection = get_email_collection()
    collection.upsert(
        ids=[email_id],
        documents=[email_text],
        embeddings=[embedding],
        metadatas=[{"label": label, "confirmed_by": confirmed_by}],
    )

    # Store in PostgreSQL for persistence (local DB — PII allowed here)
    embedding_bytes = bytes(__import__("struct").pack(f"{len(embedding)}f", *embedding))
    async with acquire() as conn:
        await conn.execute(
            """
            INSERT INTO labeled_emails (email_id, subject, snippet, embedding, stage, confirmed_by)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (email_id) DO NOTHING
            """,
            email_id,
            subject,
            snippet,
            embedding_bytes,
            label,
            confirmed_by,
        )


async def enqueue_for_labeling(
    email_id: str,
    subject: str,
    snippet: str,
    body: str,
    guessed_label: str,
    guessed_company: str | None,
    guessed_role: str | None,
) -> None:
    """Add an email to the labeling queue for human review in the dashboard."""
    # Local DB — PII allowed (email subjects/bodies stay on this machine)
    embedding = _get_embedder().embed(f"{subject} {snippet}")
    embedding_bytes = bytes(__import__("struct").pack(f"{len(embedding)}f", *embedding))
    async with acquire() as conn:
        await conn.execute(
            """
            INSERT INTO labeling_queue
                (email_id, subject, snippet, body, guessed_stage, guessed_company,
                 guessed_role, embedding)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (email_id) DO NOTHING
            """,
            email_id,
            subject,
            snippet,
            body,
            guessed_label,
            guessed_company,
            guessed_role,
            embedding_bytes,
        )
