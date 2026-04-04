"""Resume Matcher LangGraph — recall, filter, rerank, store.

Uses Sonnet for reranking, Titan v2 embeddings via Bedrock Knowledge Base for recall.
Runs on ECS alongside the FastAPI API.

Conditional edge: After recall, short-circuits to END if 0 candidates found
(saves filter + rerank + store operations).
"""

import logging
from typing import TypedDict

from langgraph.graph import StateGraph, END

from api.agents.resume_matcher.tools import (
    recall,
    rerank,
    resolve_job_ids,
    store_reports,
    structured_filter,
)

logger = logging.getLogger(__name__)


class ResumeMatcherState(TypedDict):
    resume_id: int
    resume_text: str
    resume_meta: dict | None
    target_job_id: int | None  # If set, match against this specific job only
    recall_results: list
    filtered: list
    ranked: list
    conn: object  # asyncpg connection


async def node_recall(state: ResumeMatcherState) -> dict:
    """Retrieve candidate JDs from Knowledge Base, or use targeted job."""
    target = state.get("target_job_id")
    if target is not None:
        # Targeted mode: read JD from jd_analyses for this specific job
        logger.info(
            "Resume Matcher: targeted recall for job_id=%d, resume_id=%d",
            target,
            state["resume_id"],
        )
        row = await state["conn"].fetchrow(
            "SELECT raw_jd_text FROM jd_analyses WHERE job_id = $1",
            target,
        )
        if row and row["raw_jd_text"]:
            return {
                "recall_results": [
                    {
                        "content": row["raw_jd_text"],
                        "score": 1.0,
                        "s3_uri": "",
                        "job_id": target,
                    }
                ]
            }
        # Fallback: try reading from jobs.jd_s3_key
        logger.warning("Resume Matcher: no jd_analyses for job_id=%d, skipping", target)
        return {"recall_results": []}

    logger.info("Resume Matcher: recall for resume_id=%d", state["resume_id"])
    results = await recall(state["resume_text"])
    return {"recall_results": results}


async def node_resolve_ids(state: ResumeMatcherState) -> dict:
    """Resolve KB recall results to job IDs via jd_s3_key lookup."""
    logger.info(
        "Resume Matcher: resolving job IDs for %d candidates",
        len(state["recall_results"]),
    )
    resolved = await resolve_job_ids(state["recall_results"], state["conn"])
    return {"recall_results": resolved}


async def node_filter(state: ResumeMatcherState) -> dict:
    """Apply structured filters (deal_breakers, experience)."""
    logger.info(
        "Resume Matcher: filtering %d candidates for resume_id=%d",
        len(state["recall_results"]),
        state["resume_id"],
    )
    filtered = await structured_filter(
        state["recall_results"], state["conn"], state.get("resume_meta")
    )
    return {"filtered": filtered}


async def node_rerank(state: ResumeMatcherState) -> dict:
    """Rerank filtered candidates using Sonnet."""
    logger.info(
        "Resume Matcher: reranking %d candidates for resume_id=%d",
        len(state["filtered"]),
        state["resume_id"],
    )
    ranked = await rerank(state["filtered"], state["resume_text"])
    return {"ranked": ranked}


async def node_store_reports(state: ResumeMatcherState) -> dict:
    """Store match reports in database."""
    logger.info("Resume Matcher: storing reports for resume_id=%d", state["resume_id"])
    await store_reports(state["conn"], state["resume_id"], state["ranked"])
    return {}


def recall_result_router(state: ResumeMatcherState) -> str:
    """Short-circuit to END if recall returned 0 candidates."""
    if not state.get("recall_results"):
        return "empty"
    return "resolve"


def build_graph() -> StateGraph:
    """Build and compile the Resume Matcher graph.

    Conditional edge: After recall, routes to resolve_ids (has candidates)
    or END (empty recall, skip all downstream work).
    """
    graph = StateGraph(ResumeMatcherState)

    graph.add_node("recall", node_recall)
    graph.add_node("resolve_ids", node_resolve_ids)
    graph.add_node("filter", node_filter)
    graph.add_node("rerank", node_rerank)
    graph.add_node("store_reports", node_store_reports)

    graph.set_entry_point("recall")

    # CONDITIONAL EDGE: skip pipeline if recall returns 0 candidates
    graph.add_conditional_edges(
        "recall",
        recall_result_router,
        {
            "resolve": "resolve_ids",
            "empty": END,
        },
    )

    graph.add_edge("resolve_ids", "filter")
    graph.add_edge("filter", "rerank")
    graph.add_edge("rerank", "store_reports")
    graph.add_edge("store_reports", END)

    return graph.compile()


async def run_resume_matcher(
    conn,
    resume_id: int,
    resume_text: str,
    resume_meta: dict | None = None,
    target_job_id: int | None = None,
) -> dict:
    """Entry point: match a resume against JDs.

    Args:
        conn: asyncpg connection.
        resume_id: The resume record ID.
        resume_text: Resume text content.
        resume_meta: Optional metadata (experience_years, etc.).
        target_job_id: If set, match against this specific job only (skips KB recall).

    Returns:
        Final state dict with ranked match results.
    """
    compiled = build_graph()
    result = await compiled.ainvoke(
        {
            "resume_id": resume_id,
            "resume_text": resume_text,
            "resume_meta": resume_meta,
            "target_job_id": target_job_id,
            "recall_results": [],
            "filtered": [],
            "ranked": [],
            "conn": conn,
        }
    )
    return result
