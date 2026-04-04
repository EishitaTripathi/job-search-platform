"""Phi-3 client via Ollama HTTP API.

Usage:
    response = await llm_generate("Classify this email: ...", system="You are an email classifier.")

Uses httpx (never requests — CLAUDE.md convention).
All inputs pass through sanitize_for_prompt() before injection.
"""

import os
import re

import httpx

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
MODEL = "phi3:mini"
TIMEOUT = 120.0


def sanitize_for_prompt(text: str) -> str:
    """Strip content that could manipulate LLM behavior.

    Security rule: sanitize_for_prompt() on all text injected into LLM prompts.
    """
    # Remove common prompt injection patterns
    text = re.sub(r"(?i)(ignore\s+(previous|above|all)\s+instructions)", "", text)
    text = re.sub(r"(?i)(you\s+are\s+now|new\s+instructions|system\s*:)", "", text)
    text = re.sub(r"(?i)(disregard|forget|override)\s+(everything|all|prior)", "", text)
    text = re.sub(r"(?i)(act\s+as|pretend\s+to\s+be|roleplay)", "", text)
    text = re.sub(r"(?i)(do\s+not\s+follow|stop\s+being)", "", text)
    # Strip injected code blocks that could contain instructions
    text = re.sub(r"```.*?```", "[CODE_BLOCK]", text, flags=re.DOTALL)
    # Limit length to prevent context stuffing
    return text[:8000]


async def llm_generate(
    prompt: str,
    system: str | None = None,
    temperature: float = 0.1,
    max_tokens: int = 1024,
) -> str:
    """Send a prompt to Phi-3 via Ollama and return the response text."""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            f"{OLLAMA_BASE_URL}/api/chat",
            json={
                "model": MODEL,
                "messages": messages,
                "stream": False,
                "options": {
                    "temperature": temperature,
                    "num_predict": max_tokens,
                },
            },
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"]
