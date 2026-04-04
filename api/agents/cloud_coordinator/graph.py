"""Cloud Coordinator LangGraph — routes events to downstream cloud agents.

Uses Haiku via Bedrock for routing decisions. Orchestrates the full cloud
agent pipeline: JD Analyzer -> Sponsorship Screener -> Resume Matcher,
plus Application Chat and ingestion handlers.

Routes:
- new_jd: JD Analyzer -> Sponsorship Screener -> Resume Matcher
- ingest_status: update job status
- ingest_recommendation: create job + enqueue fetch
- chat: Application Chat
"""

import asyncio
import logging
import os

import boto3
from typing import TypedDict

from langgraph.graph import StateGraph, END

from api.agents.cloud_coordinator.tools import create_run, update_run
from api.agents.jd_analyzer.graph import run_jd_analyzer

# Sponsorship screening moved to JD Ingestion Agent (screens BEFORE S3 storage)
from api.agents.resume_matcher.graph import run_resume_matcher
from api.agents.application_chat.graph import run_application_chat

logger = logging.getLogger(__name__)


class CloudCoordinatorState(TypedDict):
    event_type: str
    event_data: dict
    run_id: str
    agent_chain: list
    results: dict
    status: str
    conn: object  # asyncpg connection


async def node_route_event(state: CloudCoordinatorState) -> dict:
    """Determine which agents to dispatch based on event type."""
    event_type = state["event_type"]
    logger.info("Cloud Coordinator: routing event_type=%s", event_type)

    # Create orchestration run
    run_id = await create_run(state["conn"], event_type, state["event_data"])

    routing = {
        "new_jd": [
            "jd_analyzer",
            "resume_matcher",
        ],  # sponsorship screening in JD Ingestion Agent
        "ingest_status": ["status_update"],
        "ingest_recommendation": ["create_job"],
        "chat": ["application_chat"],
    }

    agent_chain = routing.get(event_type, [])
    if not agent_chain:
        logger.warning("Cloud Coordinator: unknown event_type=%s", event_type)

    return {"run_id": run_id, "agent_chain": agent_chain}


async def node_dispatch(state: CloudCoordinatorState) -> dict:
    """Dispatch to the appropriate agent chain."""
    conn = state["conn"]
    event_data = state["event_data"]
    agent_chain = state["agent_chain"]
    results = {}

    try:
        for agent_name in agent_chain:
            logger.info(
                "Cloud Coordinator: dispatching to %s (run=%s)",
                agent_name,
                state["run_id"],
            )

            if agent_name == "jd_analyzer":
                job_id = event_data.get("job_id")
                raw_jd_text = event_data.get("jd_text", "")
                result = await run_jd_analyzer(conn, job_id, raw_jd_text)
                results["jd_analyzer"] = {
                    "jd_analysis_id": result.get("jd_analysis_id"),
                    "fields": result.get("fields", {}),
                }
                # Pass cleaned text downstream
                event_data["cleaned_jd_text"] = result.get("cleaned_text", raw_jd_text)

            elif agent_name == "resume_matcher":
                # Match all active resumes against this specific job
                target_job_id = event_data.get("job_id")
                resumes = await conn.fetch(
                    "SELECT id, s3_key FROM resumes ORDER BY uploaded_at DESC"
                )
                if not resumes:
                    logger.info(
                        "Cloud Coordinator: skipping resume_matcher — no resumes uploaded (run=%s)",
                        state["run_id"],
                    )
                    results["resume_matcher"] = {
                        "skipped": True,
                        "reason": "no_resumes",
                    }
                    continue
                s3_bucket = os.environ.get("S3_BUCKET", "")
                s3 = boto3.client(
                    "s3", region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-2")
                )
                match_results = []
                for resume in resumes:
                    # Read resume text from S3 (via thread to avoid blocking event loop)
                    try:
                        obj = await asyncio.to_thread(
                            s3.get_object, Bucket=s3_bucket, Key=resume["s3_key"]
                        )
                        resume_text = (
                            obj["Body"].read().decode("utf-8", errors="replace")
                        )
                    except Exception:
                        logger.exception(
                            "Failed to read resume s3_key=%s", resume["s3_key"]
                        )
                        continue

                    result = await run_resume_matcher(
                        conn,
                        resume["id"],
                        resume_text,
                        target_job_id=target_job_id,
                    )
                    ranked = result.get("ranked") or []
                    match_results.append(
                        {
                            "resume_id": resume["id"],
                            "top_score": ranked[0]["overall_fit_score"]
                            if ranked
                            else 0.0,
                        }
                    )
                results["resume_matcher"] = match_results

                # Update jobs.match_score with the best score across all resumes
                if match_results and target_job_id:
                    best_score = max(m["top_score"] for m in match_results)
                    await conn.execute(
                        "UPDATE jobs SET match_score = $1 WHERE id = $2",
                        best_score,
                        target_job_id,
                    )

            elif agent_name == "application_chat":
                job_id = event_data.get("job_id")
                question = event_data.get("question", "")
                result = await run_application_chat(conn, job_id, question)
                results["application_chat"] = {"answer": result.get("answer", "")}

            elif agent_name == "status_update":
                job_id = event_data.get("job_id")
                stage = event_data.get("stage")
                await conn.execute(
                    "UPDATE jobs SET status = $1, last_updated = NOW() WHERE id = $2",
                    stage,
                    job_id,
                )
                results["status_update"] = {"job_id": job_id, "stage": stage}

            elif agent_name == "create_job":
                company = event_data.get("company")
                role = event_data.get("role")
                row = await conn.fetchrow(
                    """
                    INSERT INTO jobs (company, role, source, status)
                    VALUES ($1, $2, 'email_recommendation', 'to_apply')
                    ON CONFLICT (company, role, source) DO NOTHING
                    RETURNING id
                    """,
                    company,
                    role,
                )
                job_id = row["id"] if row else None
                results["create_job"] = {"job_id": job_id}

        return {"results": results, "status": "completed"}

    except Exception as exc:
        logger.error(
            "Cloud Coordinator: dispatch failed (run=%s): %s",
            state["run_id"],
            exc,
            exc_info=True,
        )
        return {"results": {"error": str(exc)}, "status": "failed"}


async def node_track_run(state: CloudCoordinatorState) -> dict:
    """Update orchestration run with final status and results."""
    logger.info(
        "Cloud Coordinator: tracking run %s status=%s",
        state["run_id"],
        state["status"],
    )
    await update_run(state["conn"], state["run_id"], state["status"], state["results"])
    return {}


def build_graph() -> StateGraph:
    """Build and compile the Cloud Coordinator graph."""
    graph = StateGraph(CloudCoordinatorState)

    graph.add_node("route_event", node_route_event)
    graph.add_node("dispatch", node_dispatch)
    graph.add_node("track_run", node_track_run)

    graph.set_entry_point("route_event")
    graph.add_edge("route_event", "dispatch")
    graph.add_edge("dispatch", "track_run")
    graph.add_edge("track_run", END)

    return graph.compile()


async def run_cloud_coordinator(conn, event_type: str, event_data: dict) -> dict:
    """Entry point: route and dispatch an event through the cloud agent pipeline.

    Args:
        conn: asyncpg connection.
        event_type: One of "new_jd", "ingest_status", "ingest_recommendation", "chat".
        event_data: Event-specific data dict.

    Returns:
        Final state dict with results and status.
    """
    compiled = build_graph()
    result = await compiled.ainvoke(
        {
            "event_type": event_type,
            "event_data": event_data,
            "run_id": "",
            "agent_chain": [],
            "results": {},
            "status": "pending",
            "conn": conn,
        }
    )
    return result
