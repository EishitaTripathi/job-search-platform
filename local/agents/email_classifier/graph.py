"""Email Classifier — LangGraph StateGraph with confidence-based conditional routing.

Three-way classification: irrelevant / status_update / recommendation.

Conditional edge: After classify, routes based on confidence threshold:
  - High confidence (>= 0.85): auto_store → set action → END
  - Low confidence (< 0.85): enqueue_review → END (action="skip", await human label)

Cold start: ALL emails go to labeling queue (confidence = 0).
As user labels accumulate in ChromaDB, confidence increases, more auto-stores.
"""

from typing import TypedDict

from langgraph.graph import END, StateGraph

from local.agents.email_classifier.tools import (
    classify_email,
    enqueue_for_labeling,
    store_labeled_example,
)
from local.agents.shared.tracking import track_agent_run

AUTO_CONFIDENCE_THRESHOLD = 0.85


class EmailState(TypedDict):
    """State passed through the Email Classifier graph."""

    email_id: str
    subject: str
    snippet: str
    body: str
    # Populated by classify node
    label: str
    company: str | None
    role: str | None
    urls: list[str]
    confidence: float
    # Output for downstream agents
    action: str  # skip, to_followup, to_fetch


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


async def classify_node(state: EmailState) -> dict:
    """Classify the email using RAG few-shot + Phi-3."""
    email_text = f"{state['subject']} {state['snippet']} {state['body'][:1000]}"

    with track_agent_run("email_classifier", {"email_id": state["email_id"]}):
        result = await classify_email(email_text)

    return {
        "label": result["label"],
        "company": result["company"],
        "role": result["role"],
        "urls": result["urls"],
        "confidence": result["confidence"],
    }


async def auto_store_node(state: EmailState) -> dict:
    """High confidence: auto-store in ChromaDB + PostgreSQL, set downstream action."""
    await store_labeled_example(
        email_id=state["email_id"],
        subject=state["subject"],
        snippet=state["snippet"],
        label=state["label"],
        confirmed_by="auto",
    )

    # Store body in labeling_queue (resolved) so re-label can re-route later
    from local.agents.shared.db import acquire

    async with acquire() as conn:
        await conn.execute(
            """
            INSERT INTO labeling_queue (email_id, subject, snippet, body, guessed_stage, resolved)
            VALUES ($1, $2, $3, $4, $5, TRUE)
            ON CONFLICT (email_id) DO NOTHING
            """,
            state["email_id"],
            state["subject"],
            state["snippet"],
            state["body"],
            state["label"],
        )

    # Determine downstream action based on label
    label = state["label"]
    if label == "status_update":
        return {"action": "to_followup"}
    elif label == "recommendation":
        return {"action": "to_fetch"}
    else:
        return {"action": "skip"}


async def enqueue_review_node(state: EmailState) -> dict:
    """Low confidence: enqueue for human review in the labeling dashboard."""
    await enqueue_for_labeling(
        email_id=state["email_id"],
        subject=state["subject"],
        snippet=state["snippet"],
        body=state["body"],
        guessed_label=state["label"],
        guessed_company=state.get("company"),
        guessed_role=state.get("role"),
    )
    # Don't process further until human reviews — action is skip
    return {"action": "skip"}


# ---------------------------------------------------------------------------
# Conditional routing
# ---------------------------------------------------------------------------


def confidence_router(state: EmailState) -> str:
    """Route based on classification confidence threshold."""
    if state["confidence"] >= AUTO_CONFIDENCE_THRESHOLD:
        return "high"
    return "low"


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------


def build_graph() -> StateGraph:
    """Build and compile the Email Classifier LangGraph.

    Conditional edge after classify:
      - High confidence -> auto_store (stores + sets action for downstream)
      - Low confidence -> enqueue_review (queues for human label, action=skip)

    The state["action"] field is consumed by main.py's dispatcher to chain
    downstream agents (stage_classifier, recommendation_parser, etc.).
    """
    graph = StateGraph(EmailState)

    graph.add_node("classify", classify_node)
    graph.add_node("auto_store", auto_store_node)
    graph.add_node("enqueue_review", enqueue_review_node)

    graph.set_entry_point("classify")

    # CONDITIONAL EDGE: confidence-based routing
    graph.add_conditional_edges(
        "classify",
        confidence_router,
        {
            "high": "auto_store",
            "low": "enqueue_review",
        },
    )

    graph.add_edge("auto_store", END)
    graph.add_edge("enqueue_review", END)

    return graph.compile()
