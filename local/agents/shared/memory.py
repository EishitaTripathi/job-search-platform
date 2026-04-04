"""ChromaDB client factory for email classification few-shot retrieval.

Usage:
    client = get_chroma_client()
    collection = client.get_or_create_collection("email_classifications")

Two collections:
- email_classifications: Email Classifier (3 classes: irrelevant, status_update, recommendation)
- stage_classifications: Follow-up Advisor (6 classes: applied, assessment, assignment, interview, offer, rejected)

PII stays local — ChromaDB runs in Docker, never touches AWS.
"""

import os
from typing import Optional

import chromadb


_client: Optional[chromadb.HttpClient] = None


def get_chroma_client() -> chromadb.HttpClient:
    """Return shared ChromaDB HTTP client."""
    global _client
    if _client is None:
        host = os.environ.get("CHROMADB_HOST", "localhost")
        port = int(os.environ.get("CHROMADB_PORT", "8000"))
        _client = chromadb.HttpClient(host=host, port=port, timeout=10)
    return _client


def get_email_collection() -> chromadb.Collection:
    """Collection for Email Classifier few-shot examples (3 classes)."""
    return get_chroma_client().get_or_create_collection(
        name="email_classifications",
        metadata={"description": "Email type classification few-shot examples"},
    )


def get_stage_collection() -> chromadb.Collection:
    """Collection for Follow-up Advisor stage classification (6 classes)."""
    return get_chroma_client().get_or_create_collection(
        name="stage_classifications",
        metadata={"description": "Application stage classification few-shot examples"},
    )
