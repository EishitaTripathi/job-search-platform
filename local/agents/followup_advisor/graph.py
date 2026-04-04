"""Follow-up Advisor — LangGraph StateGraph.

Simplified to daily_check mode only. Stage classification has been
extracted to the Stage Classifier agent.

Flow:
    daily_check -> send_to_pipeline -> END
"""

from typing import TypedDict

from langgraph.graph import StateGraph, END

from local.agents.followup_advisor.tools import (
    daily_check,
    send_followups_to_pipeline,
)
from local.agents.shared.tracking import track_agent_run


class FollowupState(TypedDict):
    """State for Follow-up Advisor graph."""

    recommendations: list[dict]
    sent_count: int


async def daily_check_node(state: FollowupState) -> dict:
    """Run the daily stale job check."""
    with track_agent_run("followup_advisor", {"mode": "daily_check"}):
        recs = await daily_check()
    return {"recommendations": recs}


async def send_to_pipeline_node(state: FollowupState) -> dict:
    """Send each recommendation as FollowupPayload through the pipeline."""
    sent_count = await send_followups_to_pipeline(state["recommendations"])
    return {"sent_count": sent_count}


def build_graph() -> StateGraph:
    """Build and compile the Follow-up Advisor LangGraph."""
    graph = StateGraph(FollowupState)

    graph.add_node("daily_check", daily_check_node)
    graph.add_node("send_to_pipeline", send_to_pipeline_node)

    graph.set_entry_point("daily_check")
    graph.add_edge("daily_check", "send_to_pipeline")
    graph.add_edge("send_to_pipeline", END)

    return graph.compile()
