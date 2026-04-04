"""Application Chat tools — Sonnet-powered contextual Q&A about job applications.

Gathers context from JD analysis, match reports, answer memory, and KB,
then generates answers using Sonnet with full context.
"""

import logging

from api.agents.bedrock_client import (
    SONNET,
    async_invoke_model,
    async_retrieve_from_kb,
    sanitize_for_prompt,
)

logger = logging.getLogger(__name__)


async def retrieve_context(conn, job_id: int, question: str) -> str:
    """Gather all relevant context for answering a question about a job.

    Sources:
    1. JD analysis (structured fields)
    2. Match reports (fit scores, gaps, strengths)
    3. Previous Q&A from answer_memory (conversation continuity)
    4. KB retrieval (additional JD/resume context)
    """
    parts = []

    # 1. JD analysis
    analysis = await conn.fetchrow(
        "SELECT * FROM jd_analyses WHERE job_id = $1", job_id
    )
    if analysis:
        exp = analysis["experience_range"]
        exp_str = f"{exp.lower}-{exp.upper} years" if exp else "Not specified"
        parts.append(
            f"JD ANALYSIS:\n"
            f"Role type: {analysis['role_type']}\n"
            f"Required skills: {analysis['required_skills']}\n"
            f"Preferred skills: {analysis['preferred_skills']}\n"
            f"Tech stack: {analysis['tech_stack']}\n"
            f"Experience: {exp_str}\n"
            f"Deal breakers: {analysis['deal_breakers']}"
        )

    # 2. Match reports
    matches = await conn.fetch(
        """
        SELECT mr.*, r.name AS resume_name
        FROM match_reports mr
        JOIN resumes r ON r.id = mr.resume_id
        WHERE mr.job_id = $1
        ORDER BY mr.overall_fit_score DESC
        """,
        job_id,
    )
    if matches:
        match_text = "MATCH REPORTS:\n"
        for m in matches:
            match_text += (
                f"- {m['resume_name']}: score={m['overall_fit_score']}, "
                f"category={m['fit_category']}, skill_gaps={m['skill_gaps']}\n"
                f"  Reasoning: {m['reasoning']}\n"
            )
        parts.append(match_text)

    # 3. Previous Q&A from answer_memory
    prev_qa = await conn.fetch(
        """
        SELECT question_text, answer_text FROM answer_memory
        WHERE company = (SELECT company FROM jobs WHERE id = $1)
          AND role = (SELECT role FROM jobs WHERE id = $1)
        ORDER BY created_at DESC
        LIMIT 5
        """,
        job_id,
    )
    if prev_qa:
        qa_text = "PREVIOUS Q&A:\n"
        for qa in reversed(prev_qa):  # chronological order
            qa_text += f"Q: {qa['question_text']}\nA: {qa['answer_text']}\n\n"
        parts.append(qa_text)

    # 4. KB retrieval for additional context
    job = await conn.fetchrow("SELECT company, role FROM jobs WHERE id = $1", job_id)
    if job:
        kb_query = f"{job['company']} {job['role']} {question}"
        kb_results = await async_retrieve_from_kb(kb_query, top_k=3)
        if kb_results:
            kb_text = "KNOWLEDGE BASE CONTEXT:\n"
            for r in kb_results:
                kb_text += f"- {r['content'][:500]}\n"
            parts.append(kb_text)

    return "\n\n".join(parts)


async def generate_answer(context: str, question: str) -> str:
    """Generate an answer to the question using Sonnet with gathered context."""
    system = (
        "You are an application intelligence assistant helping a job seeker understand "
        "their job applications. You have access to JD analyses, match reports, and "
        "previous Q&A history.\n\n"
        "Answer the user's question based on the provided context. Be specific and "
        "actionable. If the context doesn't contain enough information to answer, "
        "say so clearly rather than guessing.\n\n"
        "Keep answers concise but thorough. Use bullet points for lists."
    )
    user_msg = f"CONTEXT:\n{sanitize_for_prompt(context)}\n\nQUESTION: {sanitize_for_prompt(question)}"

    answer = await async_invoke_model(
        SONNET, system, user_msg, max_tokens=1024, temperature=0.3
    )
    return answer


async def store_answer_memory(conn, job_id: int, question: str, answer: str) -> None:
    """Store Q&A pair in answer_memory for conversation continuity."""
    await conn.execute(
        """
        INSERT INTO answer_memory (question_text, answer_text, company, role)
        VALUES ($2, $3,
                (SELECT company FROM jobs WHERE id = $1),
                (SELECT role FROM jobs WHERE id = $1))
        """,
        job_id,
        question,
        answer,
    )
    logger.info("Application Chat: stored answer for job_id=%d", job_id)
