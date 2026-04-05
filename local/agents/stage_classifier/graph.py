"""Stage Classifier — LangGraph StateGraph.

Extracted from Follow-up Advisor. Classifies status_update emails into
8 application stages using RAG few-shot + Phi-3.

Flow:
    classify -> route_by_confidence -> send_to_pipeline
"""

from typing import TypedDict

from langgraph.graph import StateGraph, END

from local.agents.stage_classifier.tools import (
    classify_stage,
    store_stage_example,
    enqueue_stage_review,
    lookup_job_id,
    send_status_to_pipeline,
)
from local.agents.shared.tracking import track_agent_run

AUTO_CONFIDENCE_THRESHOLD = 0.85


class StageClassifierState(TypedDict):
    """State for Stage Classifier graph."""

    email_id: str
    subject: str
    snippet: str
    body: str
    company: str | None
    role: str | None
    stage: str
    confidence: float
    job_id: int | None


async def classify_node(state: StageClassifierState) -> dict:
    """Classify application stage for a status_update email."""
    email_text = f"{state['subject']} {state['snippet']} {state['body'][:1000]}"

    with track_agent_run(
        "stage_classifier",
        {
            "email_id": state.get("email_id", ""),
        },
    ):
        result = await classify_stage(email_text)

    return {
        "stage": result["stage"],
        "confidence": result["confidence"],
    }


async def route_by_confidence_node(state: StageClassifierState) -> dict:
    """Route based on confidence: auto-store or enqueue for review."""
    if state["confidence"] >= AUTO_CONFIDENCE_THRESHOLD:
        await store_stage_example(
            email_id=state["email_id"],
            subject=state["subject"],
            snippet=state["snippet"],
            stage=state["stage"],
            confirmed_by="auto",
        )
    else:
        await enqueue_stage_review(
            email_id=state["email_id"],
            stage=state["stage"],
        )

    company = state.get("company")
    role = state.get("role")

    # Fallback: extract company/role from email if classifier missed them
    if not company or not role:
        try:
            from local.agents.shared.llm import llm_generate, sanitize_for_prompt

            extract_prompt = (
                "Extract the company name and job role from this email. "
                "Respond with ONLY a JSON object: "
                '{"company": "company name", "role": "job title"}\n\n'
                f"Subject: {sanitize_for_prompt(state['subject'])}\n"
                f"Snippet: {sanitize_for_prompt(state['snippet'][:500])}"
            )
            import json
            import re

            resp = await llm_generate(extract_prompt, temperature=0.0, max_tokens=100)
            match = re.search(r"\{[^}]+\}", resp)
            if match:
                extracted = json.loads(match.group())
                company = company or extracted.get("company")
                role = role or extracted.get("role")
        except Exception:
            pass

    # Fuzzy match company+role to find job_id locally
    job_id = await lookup_job_id(company, role)

    # If no local job but we have company+role, create via cloud recommendation
    if not job_id and company and role:
        try:
            from local.pipeline.sender import send_to_cloud_with_response
            from local.pipeline.schemas import RecommendationPayload

            payload = RecommendationPayload(company=company, role=role)
            resp = await send_to_cloud_with_response("recommendation", payload)
            if resp and resp.get("job_id"):
                job_id = resp["job_id"]
                # Create locally for future lookups
                from local.agents.shared.db import acquire

                async with acquire() as conn:
                    await conn.execute(
                        """
                        INSERT INTO jobs (id, company, role, source, status)
                        VALUES ($1, $2, $3, 'email_recommendation', 'to_apply')
                        ON CONFLICT DO NOTHING
                        """,
                        job_id,
                        company,
                        role,
                    )
        except Exception:
            import logging

            logging.getLogger(__name__).warning(
                "Failed to create cloud job for %s — %s",
                company,
                role,
            )

    return {"job_id": job_id}


async def send_to_pipeline_node(state: StageClassifierState) -> dict:
    """Send StatusPayload through the validation pipeline to cloud."""
    if state.get("job_id") is not None:
        await send_status_to_pipeline(
            job_id=state["job_id"],
            stage=state["stage"],
        )
    return {}


def build_graph() -> StateGraph:
    """Build and compile the Stage Classifier LangGraph."""
    graph = StateGraph(StageClassifierState)

    graph.add_node("classify", classify_node)
    graph.add_node("route_by_confidence", route_by_confidence_node)
    graph.add_node("send_to_pipeline", send_to_pipeline_node)

    graph.set_entry_point("classify")
    graph.add_edge("classify", "route_by_confidence")
    graph.add_edge("route_by_confidence", "send_to_pipeline")
    graph.add_edge("send_to_pipeline", END)

    return graph.compile()
