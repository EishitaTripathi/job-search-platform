"""Shared Bedrock client for cloud agents.

Uses boto3 bedrock-runtime for model invocation.
All text is sanitized before sending to models.
"""

import asyncio
import json
import logging
import os
import re

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

_client = None
_kb_client = None

BEDROCK_KB_ID = os.environ.get("BEDROCK_KB_ID", "")
AWS_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-2")

# Model IDs — cross-region inference profiles
HAIKU = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
SONNET = "us.anthropic.claude-sonnet-4-6"


def _ensure_client():
    global _client
    if _client is None:
        _client = boto3.client("bedrock-runtime", region_name=AWS_REGION)
    return _client


def _ensure_kb_client():
    global _kb_client
    if _kb_client is None:
        _kb_client = boto3.client("bedrock-agent-runtime", region_name=AWS_REGION)
    return _kb_client


def sanitize_for_prompt(text: str) -> str:
    """Strip prompt injection patterns from text before sending to LLM.

    Cloud processes external JD text, user chat queries, and KB retrieval
    results — higher-risk surface than local email processing.
    """
    if not text:
        return ""
    # Special token removal (Bedrock/Claude specific)
    text = re.sub(r"<\|.*?\|>", "", text)
    # Role marker injection
    text = re.sub(r"(?i)(system|human|assistant)\s*:", "", text)
    # Instruction override attempts
    text = re.sub(r"(?i)ignore\s+(previous|all|above)\s+instructions?", "", text)
    text = re.sub(r"(?i)(you\s+are\s+now|new\s+instructions)", "", text)
    text = re.sub(r"(?i)(disregard|forget|override)\s+(everything|all|prior)", "", text)
    # Identity manipulation
    text = re.sub(r"(?i)(act\s+as|pretend\s+to\s+be|roleplay)", "", text)
    text = re.sub(r"(?i)(do\s+not\s+follow|stop\s+being)", "", text)
    # Code block injection (could contain hidden instructions)
    text = re.sub(r"```.*?```", "[CODE_BLOCK]", text, flags=re.DOTALL)
    # XML/HTML tag injection (Claude-specific — could manipulate tool use)
    text = re.sub(
        r"</?(?:tool_use|function_call|system|thinking)[^>]*>",
        "",
        text,
        flags=re.IGNORECASE,
    )
    # Length cap — prevent context stuffing (16K for JDs, higher than local 8K for emails)
    return text[:16000].strip()


def invoke_model(
    model_id: str,
    system: str,
    user_message: str,
    max_tokens: int = 1024,
    temperature: float = 0.1,
) -> str:
    """Invoke a Bedrock model with Messages API."""
    client = _ensure_client()

    body = json.dumps(
        {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system,
            "messages": [
                {"role": "user", "content": sanitize_for_prompt(user_message)}
            ],
        }
    )

    try:
        response = client.invoke_model(
            modelId=model_id,
            contentType="application/json",
            accept="application/json",
            body=body,
        )
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code == "ResourceNotFoundException":
            raise RuntimeError(
                f"Bedrock model {model_id} is not enabled in this AWS account. "
                "Enable model access in the AWS Bedrock console under Model Access."
            ) from e
        raise

    result = json.loads(response["body"].read())
    return result["content"][0]["text"]


async def async_invoke_model(
    model_id: str,
    system: str,
    user_message: str,
    max_tokens: int = 1024,
    temperature: float = 0.1,
) -> str:
    """Async wrapper — runs sync boto3 invoke_model in a thread to avoid blocking the event loop."""
    return await asyncio.to_thread(
        invoke_model, model_id, system, user_message, max_tokens, temperature
    )


async def async_retrieve_from_kb(query: str, top_k: int = 50) -> list[dict]:
    """Async wrapper — runs sync boto3 retrieve in a thread to avoid blocking the event loop."""
    return await asyncio.to_thread(retrieve_from_kb, query, top_k)


def retrieve_from_kb(query: str, top_k: int = 50) -> list[dict]:
    """Retrieve documents from Bedrock Knowledge Base."""
    client = _ensure_kb_client()

    response = client.retrieve(
        knowledgeBaseId=BEDROCK_KB_ID,
        retrievalQuery={"text": sanitize_for_prompt(query)},
        retrievalConfiguration={
            "vectorSearchConfiguration": {"numberOfResults": top_k}
        },
    )

    results = []
    for item in response.get("retrievalResults", []):
        results.append(
            {
                "content": item.get("content", {}).get("text", ""),
                "score": item.get("score", 0.0),
                "s3_uri": item.get("location", {}).get("s3Location", {}).get("uri", ""),
            }
        )
    return results
