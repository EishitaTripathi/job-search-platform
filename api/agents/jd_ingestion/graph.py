"""JD Ingestion Agent — LangGraph with genuine conditional routing.

Replaces Lambda Fetch + Lambda Persist + Sponsorship Screener.
Screens BEFORE storing to S3, preventing KB pollution.

Conditional edges:
1. determine_strategy → adapter/url/search (fetch strategy routing)
2. screen_sponsorship → store_and_persist / mark_skipped (sponsorship gate)
3. check_resumes → match_resumes / END (resume availability)
"""

import asyncio
import json
import logging
import os
from typing import TypedDict

from langgraph.graph import END, StateGraph

from api.agents.jd_ingestion.tools import (
    determine_fetch_strategy,
    fetch_url_content,
    fetch_via_adapter,
    persist_to_rds,
    screen_sponsorship,
    search_for_jd,
    store_to_s3,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class JDIngestionState(TypedDict):
    """State for the JD Ingestion Agent graph."""

    # Input (from SQS message)
    message_body: dict
    mode: str  # adapter | url | search

    # Fetch output (single JD being processed)
    jd_text: str
    job_data: dict  # NormalizedJob dict (adapter mode) or raw fields

    # Screening
    sponsorship_status: str  # available | unavailable | unclear
    sponsorship_reasoning: str

    # Storage
    s3_key: str
    job_id: int

    # Analysis
    jd_analysis_id: int
    resumes_available: bool
    match_results: list

    # DB connection (passed through)
    conn: object


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


async def node_determine_strategy(state: JDIngestionState) -> dict:
    """Determine fetch mode from SQS message body."""
    mode = determine_fetch_strategy(state["message_body"])
    logger.info("JD Ingestion: strategy=%s", mode)
    return {"mode": mode}


async def node_fetch_adapter(state: JDIngestionState) -> dict:
    """Fetch JDs via source adapter. Returns first job for processing."""
    body = state["message_body"]
    source = body["source"]
    params = body.get("params", {})
    jobs = await asyncio.to_thread(fetch_via_adapter, source, params)
    if not jobs:
        logger.info("JD Ingestion: adapter %s returned 0 jobs", source)
        return {"jd_text": "", "job_data": {}}
    # Process first job (SQS consumer iterates for batch)
    job = jobs[0]
    jd_text = (
        job.get("raw_json", {}).get("description", "")
        if isinstance(job.get("raw_json"), dict)
        else str(job)
    )
    return {"jd_text": jd_text, "job_data": job}


async def node_fetch_url(state: JDIngestionState) -> dict:
    """Fetch JD from a direct URL."""
    url = state["message_body"]["url"]
    jd_text = await asyncio.to_thread(fetch_url_content, url)
    return {
        "jd_text": jd_text,
        "job_data": {
            "company": "Unknown",
            "role": "Unknown",
            "source": "url",
            "ats_url": url,
        },
    }


async def node_search_ats(state: JDIngestionState) -> dict:
    """Search ATS board APIs for a matching JD."""
    body = state["message_body"]
    company = body["company"]
    role = body.get("role", "")
    match = await asyncio.to_thread(search_for_jd, company, role)
    if match:
        ats_url = match.get("ats_url", "")
        jd_text = await asyncio.to_thread(fetch_url_content, ats_url) if ats_url else ""
        return {"jd_text": jd_text, "job_data": match}
    logger.warning("JD Ingestion: no match for %s — %s", company, role)
    return {
        "jd_text": "",
        "job_data": {"company": company, "role": role, "source": "search"},
    }


async def node_screen_sponsorship(state: JDIngestionState) -> dict:
    """Screen JD for sponsorship exclusion using Haiku LLM."""
    jd_text = state["jd_text"]
    if not jd_text or len(jd_text) < 50:
        # Too short to screen — assume available
        return {
            "sponsorship_status": "available",
            "sponsorship_reasoning": "JD text too short to screen",
        }
    result = await screen_sponsorship(jd_text)
    return {
        "sponsorship_status": result.get("sponsorship_status", "available"),
        "sponsorship_reasoning": result.get("reasoning", ""),
    }


async def node_store_and_persist(state: JDIngestionState) -> dict:
    """Store qualified JD to S3 and persist to RDS. Runs JD Analyzer."""
    conn = state["conn"]
    jd_text = state["jd_text"]
    job_data = state["job_data"]

    # Store to S3 via thread (sync boto3 — triggers Bedrock KB indexing)
    s3_key = await asyncio.to_thread(
        store_to_s3, jd_text, job_data if job_data.get("source") else None
    )

    # Persist to RDS
    job_id = await persist_to_rds(
        conn,
        company=job_data.get("company", "Unknown"),
        role=job_data.get("role", "Unknown"),
        source=job_data.get("source", "unknown"),
        s3_key=s3_key or "",
        ats_url=job_data.get("ats_url"),
        raw_json=job_data.get("raw_json"),
        date_posted=job_data.get("date_posted"),
    )

    if not job_id:
        logger.info("JD Ingestion: job already exists (dedup), skipping analysis")
        return {"s3_key": s3_key or "", "job_id": 0, "jd_analysis_id": 0}

    # Empty JD text — enqueue ATS search if company/role available
    if not jd_text or not jd_text.strip():
        company = job_data.get("company", "")
        role = job_data.get("role", "")
        if company and company != "Unknown" and role and role != "Unknown":
            # Re-enqueue as search mode so node_search_ats can find the JD
            import boto3

            try:
                sqs = boto3.client(
                    "sqs", region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-2")
                )
                queue_url = sqs.get_queue_url(
                    QueueName=os.environ.get(
                        "SQS_QUEUE_NAME", "job-search-platform-jd-scrape-queue"
                    )
                )["QueueUrl"]
                sqs.send_message(
                    QueueUrl=queue_url,
                    MessageBody=json.dumps(
                        {"job_id": str(job_id), "company": company, "role": role}
                    ),
                )
                logger.info(
                    "JD Ingestion: empty JD, enqueued ATS search for job_id=%d (%s — %s)",
                    job_id,
                    company,
                    role,
                )
            except Exception:
                logger.exception(
                    "JD Ingestion: failed to enqueue ATS search for job_id=%d", job_id
                )
        else:
            logger.warning(
                "JD Ingestion: empty JD text for job_id=%d, skipping analysis", job_id
            )
            await conn.execute(
                "UPDATE jobs SET analysis_status = 'skipped', analysis_error = 'Empty JD text' WHERE id = $1",
                job_id,
            )
        return {"s3_key": s3_key or "", "job_id": job_id, "jd_analysis_id": 0}

    # Run JD Analyzer
    from api.agents.jd_analyzer.graph import run_jd_analyzer

    result = await run_jd_analyzer(conn, job_id, jd_text)
    jd_analysis_id = result.get("jd_analysis_id", 0)

    # Mark analysis completed
    await conn.execute(
        "UPDATE jobs SET analysis_status = 'completed' WHERE id = $1",
        job_id,
    )

    return {"s3_key": s3_key or "", "job_id": job_id, "jd_analysis_id": jd_analysis_id}


async def node_check_resumes(state: JDIngestionState) -> dict:
    """Check if any resumes are uploaded for matching."""
    conn = state["conn"]
    count = await conn.fetchval("SELECT COUNT(*) FROM resumes")
    return {"resumes_available": count > 0}


async def node_match_resumes(state: JDIngestionState) -> dict:
    """Run Resume Matcher for all uploaded resumes against this job."""
    import os

    import boto3

    from api.agents.resume_matcher.graph import run_resume_matcher

    conn = state["conn"]
    job_id = state["job_id"]
    if not job_id:
        return {"match_results": []}

    resumes = await conn.fetch(
        "SELECT id, s3_key FROM resumes ORDER BY uploaded_at DESC"
    )
    s3_bucket = os.environ.get("S3_BUCKET", "")
    s3 = boto3.client(
        "s3", region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-2")
    )

    match_results = []
    for resume in resumes:
        try:
            obj = await asyncio.to_thread(
                s3.get_object, Bucket=s3_bucket, Key=resume["s3_key"]
            )
            resume_text = obj["Body"].read().decode("utf-8", errors="replace")
            result = await run_resume_matcher(
                conn, resume["id"], resume_text, target_job_id=job_id
            )
            ranked = result.get("ranked") or []
            match_results.append(
                {
                    "resume_id": resume["id"],
                    "top_score": ranked[0]["overall_fit_score"] if ranked else 0.0,
                }
            )
        except Exception:
            logger.exception("Failed to match resume %s", resume["s3_key"])
            continue

    # Update jobs.match_score with best score
    if match_results and job_id:
        best = max(m["top_score"] for m in match_results)
        await conn.execute(
            "UPDATE jobs SET match_score = $1 WHERE id = $2", best, job_id
        )

    return {"match_results": match_results}


async def node_mark_skipped(state: JDIngestionState) -> dict:
    """Mark a disqualified JD as skipped. Does NOT store to S3 (no KB pollution)."""
    conn = state["conn"]
    job_data = state["job_data"]
    company = job_data.get("company", "Unknown")
    role = job_data.get("role", "Unknown")

    logger.info(
        "JD Ingestion: marking %s — %s as skipped (sponsorship: %s)",
        company,
        role,
        state.get("sponsorship_reasoning", ""),
    )

    # Create a minimal job record marked as skipped (for tracking/reporting)
    await conn.execute(
        """
        INSERT INTO jobs (company, role, source, analysis_status, analysis_error)
        VALUES ($1, $2, $3, 'skipped', $4)
        ON CONFLICT (company, role, source) DO UPDATE SET
            analysis_status = 'skipped',
            analysis_error = $4
        """,
        company,
        role,
        job_data.get("source", "unknown"),
        f"Sponsorship unavailable: {state.get('sponsorship_reasoning', '')}",
    )

    return {}


# ---------------------------------------------------------------------------
# Conditional routing functions
# ---------------------------------------------------------------------------


def strategy_router(state: JDIngestionState) -> str:
    """Route to the appropriate fetch node based on message type."""
    return state["mode"]  # "adapter" | "url" | "search"


def sponsorship_router(state: JDIngestionState) -> str:
    """Route based on sponsorship screening result."""
    if state.get("sponsorship_status") == "unavailable":
        return "disqualified"
    return "qualified"


def resume_router(state: JDIngestionState) -> str:
    """Route based on resume availability."""
    if state.get("resumes_available"):
        return "match"
    return "skip"


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------


def build_graph() -> StateGraph:
    """Build and compile the JD Ingestion Agent graph.

    Three conditional edges:
    1. strategy_router: adapter / url / search
    2. sponsorship_router: qualified / disqualified
    3. resume_router: match / skip
    """
    graph = StateGraph(JDIngestionState)

    # Nodes
    graph.add_node("determine_strategy", node_determine_strategy)
    graph.add_node("fetch_adapter", node_fetch_adapter)
    graph.add_node("fetch_url", node_fetch_url)
    graph.add_node("search_ats", node_search_ats)
    graph.add_node("screen_sponsorship", node_screen_sponsorship)
    graph.add_node("store_and_persist", node_store_and_persist)
    graph.add_node("check_resumes", node_check_resumes)
    graph.add_node("match_resumes", node_match_resumes)
    graph.add_node("mark_skipped", node_mark_skipped)

    # Entry
    graph.set_entry_point("determine_strategy")

    # CONDITIONAL EDGE 1: Fetch strategy routing
    graph.add_conditional_edges(
        "determine_strategy",
        strategy_router,
        {
            "adapter": "fetch_adapter",
            "url": "fetch_url",
            "search": "search_ats",
        },
    )

    # All fetch nodes converge at screening
    graph.add_edge("fetch_adapter", "screen_sponsorship")
    graph.add_edge("fetch_url", "screen_sponsorship")
    graph.add_edge("search_ats", "screen_sponsorship")

    # CONDITIONAL EDGE 2: Sponsorship gate
    graph.add_conditional_edges(
        "screen_sponsorship",
        sponsorship_router,
        {
            "qualified": "store_and_persist",
            "disqualified": "mark_skipped",
        },
    )

    # Qualified path continues to resume check
    graph.add_edge("store_and_persist", "check_resumes")

    # CONDITIONAL EDGE 3: Resume availability
    graph.add_conditional_edges(
        "check_resumes",
        resume_router,
        {
            "match": "match_resumes",
            "skip": END,
        },
    )

    graph.add_edge("match_resumes", END)
    graph.add_edge("mark_skipped", END)

    return graph.compile()


async def run_jd_ingestion(conn, message_body: dict) -> dict:
    """Entry point: process a single JD through the ingestion pipeline.

    Called by the SQS consumer for each message (or each job in adapter batch).
    """
    compiled = build_graph()
    result = await compiled.ainvoke(
        {
            "message_body": message_body,
            "mode": "",
            "jd_text": "",
            "job_data": {},
            "sponsorship_status": "available",
            "sponsorship_reasoning": "",
            "s3_key": "",
            "job_id": 0,
            "jd_analysis_id": 0,
            "resumes_available": False,
            "match_results": [],
            "conn": conn,
        }
    )
    return result
