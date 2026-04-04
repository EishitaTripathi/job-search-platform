"""JD Analyzer LangGraph — strips boilerplate, extracts fields, stores analysis.

Uses Haiku via Bedrock. Runs on ECS alongside the FastAPI API.
"""

import logging
from typing import TypedDict

from langgraph.graph import StateGraph, END

from api.agents.jd_analyzer.tools import (
    extract_fields,
    store_jd_analysis,
    strip_boilerplate,
)

logger = logging.getLogger(__name__)


class JDAnalyzerState(TypedDict):
    job_id: int
    raw_jd_text: str
    cleaned_text: str
    fields: dict
    jd_analysis_id: int | None
    conn: object  # asyncpg connection — not serialized, passed through state


async def node_strip_boilerplate(state: JDAnalyzerState) -> dict:
    """Strip benefits/legal/salary boilerplate from raw JD text."""
    logger.info("JD Analyzer: stripping boilerplate for job_id=%d", state["job_id"])
    cleaned = await strip_boilerplate(state["raw_jd_text"])
    return {"cleaned_text": cleaned}


async def node_extract_fields(state: JDAnalyzerState) -> dict:
    """Extract structured fields from cleaned JD text."""
    logger.info("JD Analyzer: extracting fields for job_id=%d", state["job_id"])
    fields = await extract_fields(state["cleaned_text"])
    return {"fields": fields}


async def node_store_analysis(state: JDAnalyzerState) -> dict:
    """Store extracted fields in jd_analyses table."""
    logger.info("JD Analyzer: storing analysis for job_id=%d", state["job_id"])
    jd_analysis_id = await store_jd_analysis(
        state["conn"],
        state["job_id"],
        state["fields"],
        raw_jd_text=state.get("raw_jd_text", ""),
    )
    return {"jd_analysis_id": jd_analysis_id}


def build_graph() -> StateGraph:
    """Build and compile the JD Analyzer graph."""
    graph = StateGraph(JDAnalyzerState)

    graph.add_node("strip_boilerplate", node_strip_boilerplate)
    graph.add_node("extract_fields", node_extract_fields)
    graph.add_node("store_analysis", node_store_analysis)

    graph.set_entry_point("strip_boilerplate")
    graph.add_edge("strip_boilerplate", "extract_fields")
    graph.add_edge("extract_fields", "store_analysis")
    graph.add_edge("store_analysis", END)

    return graph.compile()


async def run_jd_analyzer(conn, job_id: int, raw_jd_text: str) -> dict:
    """Entry point: analyze a JD and store results.

    Args:
        conn: asyncpg connection (from the API's pool).
        job_id: The job record ID.
        raw_jd_text: Raw job description text.

    Returns:
        Final state dict with extracted fields and jd_analysis_id.
    """
    compiled = build_graph()
    result = await compiled.ainvoke(
        {
            "job_id": job_id,
            "raw_jd_text": raw_jd_text,
            "cleaned_text": "",
            "fields": {},
            "jd_analysis_id": None,
            "conn": conn,
        }
    )
    return result
