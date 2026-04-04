"""Dispatch helpers — route classified emails to downstream agents.

Moved from coordinator/tools.py. These functions are called by main.py
after Email Classifier determines the email label.
"""

import logging

logger = logging.getLogger(__name__)

# Stages that trigger Deadline Tracker after Stage Classifier
DEADLINE_STAGES = {"assessment", "assignment", "interview"}


async def dispatch_status_update(
    email_id: str,
    subject: str,
    snippet: str,
    body: str,
    company: str | None,
    role: str | None,
) -> dict:
    """Dispatch a status_update email through Stage Classifier and optionally Deadline Tracker."""
    from local.agents.stage_classifier.graph import (
        build_graph as build_stage_classifier,
    )
    from local.agents.deadline_tracker.graph import (
        build_graph as build_deadline_tracker,
    )

    # Run Stage Classifier
    classifier = build_stage_classifier()
    stage_result = await classifier.ainvoke(
        {
            "email_id": email_id,
            "subject": subject,
            "snippet": snippet,
            "body": body,
            "company": company,
            "role": role,
            "stage": "",
            "confidence": 0.0,
            "job_id": None,
        }
    )

    result = {
        "stage": stage_result.get("stage", ""),
        "confidence": stage_result.get("confidence", 0.0),
        "job_id": stage_result.get("job_id"),
    }

    # If stage warrants deadline extraction, run Deadline Tracker
    if stage_result.get("stage") in DEADLINE_STAGES and stage_result.get("job_id"):
        tracker = build_deadline_tracker()
        deadline_result = await tracker.ainvoke(
            {
                "email_id": email_id,
                "body": body,
                "job_id": stage_result["job_id"],
                "deadlines_found": [],
                "_stage": stage_result["stage"],
            }
        )
        result["deadlines_found"] = deadline_result.get("deadlines_found", [])

    return result


async def dispatch_recommendation(
    email_id: str,
    subject: str,
    body: str,
) -> dict:
    """Dispatch a recommendation email through Recommendation Parser."""
    from local.agents.recommendation_parser.graph import build_graph as build_rec_parser

    parser = build_rec_parser()
    result = await parser.ainvoke(
        {
            "email_id": email_id,
            "subject": subject,
            "body": body,
            "companies": [],
            "roles": [],
            "sent_count": 0,
        }
    )

    return {
        "companies": result.get("companies", []),
        "roles": result.get("roles", []),
        "sent_count": result.get("sent_count", 0),
    }
