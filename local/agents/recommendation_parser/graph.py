"""Recommendation Parser — LangGraph StateGraph.

Receives 'recommendation' emails from Email Classifier, extracts
{company, role} pairs using Phi-3, and sends each through the
validation pipeline.

Flow:
    extract_entities -> validate_and_send -> END
"""

from typing import TypedDict

from langgraph.graph import StateGraph, END

from local.agents.recommendation_parser.tools import (
    extract_company_role_pairs,
    validate_and_send_recommendations,
)
from local.agents.shared.tracking import track_agent_run


class RecommendationParserState(TypedDict):
    """State for Recommendation Parser graph."""

    email_id: str
    subject: str
    body: str
    companies: list[str]
    roles: list[str]
    sent_count: int


async def extract_entities_node(state: RecommendationParserState) -> dict:
    """Extract company/role pairs from recommendation email."""
    with track_agent_run(
        "recommendation_parser",
        {
            "email_id": state.get("email_id", ""),
        },
    ):
        pairs = await extract_company_role_pairs(state["body"])

    companies = [p["company"] for p in pairs]
    roles = [p["role"] for p in pairs]
    return {"companies": companies, "roles": roles}


async def validate_and_send_node(state: RecommendationParserState) -> dict:
    """Validate each pair and send through pipeline."""
    pairs = [
        {"company": c, "role": r} for c, r in zip(state["companies"], state["roles"])
    ]
    sent_count = await validate_and_send_recommendations(pairs)
    return {"sent_count": sent_count}


def build_graph() -> StateGraph:
    """Build and compile the Recommendation Parser LangGraph."""
    graph = StateGraph(RecommendationParserState)

    graph.add_node("extract_entities", extract_entities_node)
    graph.add_node("validate_and_send", validate_and_send_node)

    graph.set_entry_point("extract_entities")
    graph.add_edge("extract_entities", "validate_and_send")
    graph.add_edge("validate_and_send", END)

    return graph.compile()
