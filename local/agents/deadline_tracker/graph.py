"""Deadline Tracker — LangGraph StateGraph.

Runs after Stage Classifier when stage is assessment, assignment, or interview.
Extracts concrete deadlines from email body using Phi-3 and includes them
in StatusPayload when sending through the pipeline.

Flow:
    parse_deadlines -> send_to_pipeline -> END
"""

from typing import TypedDict

from langgraph.graph import StateGraph, END

from local.agents.deadline_tracker.tools import (
    extract_deadlines,
    send_deadlines_to_pipeline,
)
from local.agents.shared.tracking import track_agent_run


class DeadlineTrackerState(TypedDict):
    """State for Deadline Tracker graph."""

    email_id: str
    body: str
    job_id: int | None
    deadlines_found: list[dict]


async def parse_deadlines_node(state: DeadlineTrackerState) -> dict:
    """Extract deadlines from email body using Phi-3."""
    with track_agent_run(
        "deadline_tracker",
        {
            "email_id": state.get("email_id", ""),
        },
    ):
        deadlines = await extract_deadlines(state["body"])

    return {"deadlines_found": deadlines}


async def send_to_pipeline_node(state: DeadlineTrackerState) -> dict:
    """Send each deadline through the validation pipeline."""
    if state.get("job_id") is not None and state.get("deadlines_found"):
        await send_deadlines_to_pipeline(
            job_id=state["job_id"],
            stage=state.get("_stage", "assessment"),  # passed via extra context
            deadlines=state["deadlines_found"],
        )
    return {}


def build_graph() -> StateGraph:
    """Build and compile the Deadline Tracker LangGraph."""
    graph = StateGraph(DeadlineTrackerState)

    graph.add_node("parse_deadlines", parse_deadlines_node)
    graph.add_node("send_to_pipeline", send_to_pipeline_node)

    graph.set_entry_point("parse_deadlines")
    graph.add_edge("parse_deadlines", "send_to_pipeline")
    graph.add_edge("send_to_pipeline", END)

    return graph.compile()
