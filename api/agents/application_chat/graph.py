"""Application Chat LangGraph — contextual Q&A about job applications.

Uses Sonnet via Bedrock. Gathers context from multiple sources,
generates an answer, and stores in answer_memory for continuity.
"""

import logging
from typing import TypedDict

from langgraph.graph import StateGraph, END

from api.agents.application_chat.tools import (
    generate_answer,
    retrieve_context,
    store_answer_memory,
)

logger = logging.getLogger(__name__)


class ApplicationChatState(TypedDict):
    job_id: int
    question: str
    context: str
    answer: str
    conn: object  # asyncpg connection


async def node_retrieve_context(state: ApplicationChatState) -> dict:
    """Gather context from JD analysis, match reports, answer memory, and KB."""
    logger.info("Application Chat: retrieving context for job_id=%d", state["job_id"])
    context = await retrieve_context(state["conn"], state["job_id"], state["question"])
    return {"context": context}


async def node_generate_answer(state: ApplicationChatState) -> dict:
    """Generate answer using Sonnet with gathered context."""
    logger.info("Application Chat: generating answer for job_id=%d", state["job_id"])
    answer = await generate_answer(state["context"], state["question"])
    return {"answer": answer}


async def node_store_memory(state: ApplicationChatState) -> dict:
    """Store Q&A pair in answer_memory."""
    logger.info("Application Chat: storing memory for job_id=%d", state["job_id"])
    await store_answer_memory(
        state["conn"], state["job_id"], state["question"], state["answer"]
    )
    return {}


def build_graph() -> StateGraph:
    """Build and compile the Application Chat graph."""
    graph = StateGraph(ApplicationChatState)

    graph.add_node("retrieve_context", node_retrieve_context)
    graph.add_node("generate_answer", node_generate_answer)
    graph.add_node("store_memory", node_store_memory)

    graph.set_entry_point("retrieve_context")
    graph.add_edge("retrieve_context", "generate_answer")
    graph.add_edge("generate_answer", "store_memory")
    graph.add_edge("store_memory", END)

    return graph.compile()


async def run_application_chat(conn, job_id: int, question: str) -> dict:
    """Entry point: answer a question about a specific job application.

    Args:
        conn: asyncpg connection.
        job_id: The job record ID.
        question: User's question.

    Returns:
        Final state dict with answer.
    """
    compiled = build_graph()
    result = await compiled.ainvoke(
        {
            "job_id": job_id,
            "question": question,
            "context": "",
            "answer": "",
            "conn": conn,
        }
    )
    return result
