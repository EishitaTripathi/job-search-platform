"""Smoke test for local agents — run against Docker Compose stack.

Usage:
    python scripts/test_local_agents.py

Requires: postgres, ollama (with phi3:mini), chromadb running via docker-compose.

Updated: removed JD Analyzer, Resume Matcher, Fetch Agent (moved to cloud).
Added: Stage Classifier, Recommendation Parser, Deadline Tracker.
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://jobsearch:localdev@localhost:5433/jobsearch",  # pragma: allowlist secret
)
os.environ.setdefault("OLLAMA_BASE_URL", "http://localhost:11434")
os.environ.setdefault("CHROMADB_HOST", "localhost")
os.environ.setdefault("CHROMADB_PORT", "8000")
os.environ.setdefault("MLFLOW_TRACKING_URI", "http://localhost:5001")
os.environ.setdefault("ONNX_MODEL_PATH", "local/models/all-MiniLM-L6-v2")

# These are integration smoke tests that require live Docker Compose services.
# Mark all tests so pytest skips them unless explicitly requested: pytest -m smoke
pytestmark = [pytest.mark.smoke, pytest.mark.asyncio]


async def test_embedder():
    """Test ONNX embedder produces 384-dim vectors."""
    from local.agents.shared.embedder import LocalEmbedder

    embedder = LocalEmbedder()
    vec = embedder.embed("Senior Backend Engineer at Anthropic")
    assert len(vec) == 384, f"Expected 384 dims, got {len(vec)}"
    print(f"  Embedder: OK (384-dim vector, first 5: {[round(v, 4) for v in vec[:5]]})")


async def test_chromadb():
    """Test ChromaDB connection and collection creation."""
    from local.agents.shared.memory import get_chroma_client, get_email_collection

    client = get_chroma_client()
    heartbeat = client.heartbeat()
    assert heartbeat, "ChromaDB heartbeat failed"

    collection = get_email_collection()
    assert collection.name == "email_classifications"
    print(f"  ChromaDB: OK (heartbeat={heartbeat}, collection={collection.name})")


async def test_db():
    """Test asyncpg connection to local postgres."""
    from local.agents.shared.db import get_pool, acquire, close_pool

    await get_pool()
    async with acquire() as conn:
        result = await conn.fetchval("SELECT 1")
        assert result == 1

        # Check tables exist
        tables = await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        )
        table_names = [r["tablename"] for r in tables]
        print(
            f"  Database: OK ({len(table_names)} tables: {', '.join(sorted(table_names)[:5])}...)"
        )

    await close_pool()


async def test_ollama():
    """Test Ollama is running and phi3:mini is available."""
    import httpx

    base_url = os.environ["OLLAMA_BASE_URL"]
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{base_url}/api/tags")
        models = [m["name"] for m in resp.json().get("models", [])]
        assert any(
            "phi3" in m for m in models
        ), f"phi3:mini not found, available: {models}"
        print(f"  Ollama: OK (models: {models})")


async def test_llm_generate():
    """Test LLM generation via Ollama."""
    from local.agents.shared.llm import llm_generate

    result = await llm_generate(
        "Respond with exactly one word: hello",
        temperature=0.0,
        max_tokens=10,
    )
    assert len(result) > 0, "Empty LLM response"
    print(f"  LLM generate: OK (response: {result.strip()[:50]})")


async def test_email_classifier():
    """Test Email Classifier graph with a sample email."""
    from local.agents.email_classifier.graph import build_graph

    graph = build_graph()
    state = {
        "email_id": "test-001",
        "subject": "Your application to Anthropic - Next Steps",
        "snippet": "Thank you for your application. We'd like to invite you for an interview.",
        "body": "Dear candidate, Thank you for your application to the Software Engineer position at Anthropic. We were impressed by your background and would like to invite you for a technical interview next week.",
        "label": "",
        "company": None,
        "role": None,
        "urls": [],
        "confidence": 0.0,
        "action": "",
    }
    result = await graph.ainvoke(state)
    print(
        f"  Email Classifier: OK (label={result['label']}, confidence={result['confidence']:.2f}, action={result['action']})"
    )


async def test_stage_classifier():
    """Test Stage Classifier graph with a sample status_update email."""
    from local.agents.stage_classifier.graph import build_graph

    graph = build_graph()
    state = {
        "email_id": "test-sc-001",
        "subject": "Interview Scheduled - Software Engineer at Anthropic",
        "snippet": "We'd like to schedule your technical interview for next Tuesday.",
        "body": "Dear candidate, We are pleased to invite you for a technical interview for the Software Engineer position at Anthropic. Please select a time slot from the following options.",
        "company": "Anthropic",
        "role": "Software Engineer",
        "stage": "",
        "confidence": 0.0,
        "job_id": None,
    }
    result = await graph.ainvoke(state)
    print(
        f"  Stage Classifier: OK (stage={result['stage']}, confidence={result['confidence']:.2f})"
    )


async def test_recommendation_parser():
    """Test Recommendation Parser graph with a sample recommendation email."""
    from local.agents.recommendation_parser.graph import build_graph

    graph = build_graph()
    state = {
        "email_id": "test-rp-001",
        "subject": "Check out these roles!",
        "body": "Hey, I saw these openings that might interest you: Software Engineer at Stripe, and ML Engineer at Anthropic.",
        "companies": [],
        "roles": [],
        "sent_count": 0,
    }
    result = await graph.ainvoke(state)
    print(
        f"  Recommendation Parser: OK (companies={result['companies']}, sent={result['sent_count']})"
    )


async def test_deadline_tracker():
    """Test Deadline Tracker graph with a sample email containing deadlines."""
    from local.agents.deadline_tracker.graph import build_graph

    graph = build_graph()
    state = {
        "email_id": "test-dt-001",
        "body": "Please complete the coding assessment by March 30, 2026. The link will expire after that date.",
        "job_id": None,
        "deadlines_found": [],
    }
    result = await graph.ainvoke(state)
    print(f"  Deadline Tracker: OK (deadlines_found={result['deadlines_found']})")


async def test_followup_advisor():
    """Test Follow-up Advisor daily_check mode (no emails needed)."""
    from local.agents.followup_advisor.graph import build_graph

    graph = build_graph()
    state = {
        "recommendations": [],
        "sent_count": 0,
    }
    result = await graph.ainvoke(state)
    print(
        f"  Follow-up Advisor: OK (recommendations={len(result['recommendations'])}, sent={result['sent_count']})"
    )


async def main():
    print("\n=== Local Agent Smoke Tests ===\n")

    tests = [
        ("ONNX Embedder", test_embedder),
        ("ChromaDB", test_chromadb),
        ("Database", test_db),
        ("Ollama", test_ollama),
        ("LLM Generate", test_llm_generate),
        ("Email Classifier", test_email_classifier),
        ("Stage Classifier", test_stage_classifier),
        ("Recommendation Parser", test_recommendation_parser),
        ("Deadline Tracker", test_deadline_tracker),
        ("Follow-up Advisor", test_followup_advisor),
    ]

    passed = 0
    failed = 0
    for name, test_fn in tests:
        try:
            await test_fn()
            passed += 1
        except Exception as e:
            print(f"  {name}: FAILED ({e})")
            failed += 1

    print(f"\n=== Results: {passed} passed, {failed} failed ===\n")
    return failed == 0


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
