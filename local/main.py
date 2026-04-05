"""Entry point for local services -- APScheduler + resume upload service.

Schedules (when Gmail is configured):
- Email check: every 2 hours
- Daily follow-up: 9:05am UTC

Without Gmail credentials, the scheduler still starts but skips email-dependent
jobs. Resume upload service always runs on port 8001.
"""

import asyncio
import logging
import os

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from local.agents.followup_advisor.graph import build_graph as build_followup_advisor
from local.agents.shared.tracking import (
    create_orchestration_run,
    update_orchestration_run,
)
from local.agents.shared.db import get_pool, close_pool

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
logger = logging.getLogger("main")


def _gmail_configured() -> bool:
    """Check if Gmail credentials exist on disk."""
    creds_path = os.environ.get(
        "GMAIL_CREDENTIALS_PATH", "credentials/credentials.json"
    )
    token_path = os.environ.get("GMAIL_TOKEN_PATH", "credentials/token.json")
    return os.path.exists(creds_path) or os.path.exists(token_path)


async def email_check():
    """Fetch unread emails, classify each through Email Classifier.

    Routes based on classification label:
    - status_update -> Stage Classifier -> Deadline Tracker (if applicable)
    - recommendation -> Recommendation Parser
    - irrelevant -> skip
    """
    # Lazy imports — only loaded when Gmail is actually configured
    from local.gmail.auth import get_gmail_service, fetch_recent_emails
    from local.agents.email_classifier.graph import (
        build_graph as build_email_classifier,
    )
    from local.agents.shared.dispatch import (
        dispatch_status_update,
        dispatch_recommendation,
    )

    logger.info("Starting email check")
    run_id = await create_orchestration_run(
        event_type="email_check",
        event_source="scheduler",
        agent_chain=["email_classifier"],
    )
    try:
        service = get_gmail_service()
        emails = fetch_recent_emails(service)
        logger.info(f"Fetched {len(emails)} unread emails")

        classifier = build_email_classifier()
        processed = 0

        for email in emails:
            state = {
                "email_id": email["email_id"],
                "subject": email["subject"],
                "snippet": email["snippet"],
                "body": email["body"],
                "label": "",
                "company": None,
                "role": None,
                "urls": [],
                "confidence": 0.0,
                "action": "",
            }

            result = await classifier.ainvoke(state)
            logger.info(
                f"Email {email['email_id']}: label={result['label']} "
                f"confidence={result['confidence']:.2f} action={result['action']}"
            )

            # Route to appropriate agent based on classification
            if result["action"] == "to_followup":
                stage_result = await dispatch_status_update(
                    email_id=email["email_id"],
                    subject=email["subject"],
                    snippet=email["snippet"],
                    body=email["body"],
                    company=result.get("company"),
                    role=result.get("role"),
                )
                logger.info(
                    f"Stage Classifier: email={email['email_id']} "
                    f"stage={stage_result.get('stage')} "
                    f"confidence={stage_result.get('confidence', 0):.2f} "
                    f"job_id={stage_result.get('job_id')}"
                )
                if stage_result.get("deadlines_found"):
                    logger.info(
                        f"Deadline Tracker: found {len(stage_result['deadlines_found'])} "
                        f"deadlines for email={email['email_id']}"
                    )

            elif result["action"] == "to_fetch":
                rec_result = await dispatch_recommendation(
                    email_id=email["email_id"],
                    subject=email["subject"],
                    body=email["body"],
                )
                logger.info(
                    f"Recommendation Parser: email={email['email_id']} "
                    f"extracted {len(rec_result.get('companies', []))} pairs, "
                    f"sent {rec_result.get('sent_count', 0)} to cloud"
                )

            processed += 1

        await update_orchestration_run(
            run_id,
            "completed",
            agent_results={
                "emails_fetched": len(emails),
                "emails_processed": processed,
            },
        )

    except Exception:
        logger.exception("Email check failed")
        await update_orchestration_run(
            run_id, "failed", error=str("Email check failed")
        )


async def daily_followup():
    """Run Follow-up Advisor daily check for stale jobs."""
    logger.info("Starting daily follow-up check")
    run_id = await create_orchestration_run(
        event_type="daily_followup",
        event_source="scheduler",
        agent_chain=["followup_advisor"],
    )
    try:
        followup = build_followup_advisor()
        state = {
            "recommendations": [],
            "sent_count": 0,
        }
        result = await followup.ainvoke(state)
        logger.info(
            f"Generated {len(result['recommendations'])} follow-up recommendations, "
            f"sent {result.get('sent_count', 0)} to cloud"
        )
        await update_orchestration_run(
            run_id,
            "completed",
            agent_results={
                "recommendations_count": len(result.get("recommendations", [])),
                "sent_count": result.get("sent_count", 0),
            },
        )
    except Exception:
        logger.exception("Daily follow-up check failed")
        await update_orchestration_run(
            run_id, "failed", error="Daily follow-up check failed"
        )


async def main():
    """Start scheduler and resume upload service."""
    # Initialize DB pool
    await get_pool()
    logger.info("Database pool initialized")

    # Set up scheduler
    scheduler = AsyncIOScheduler()

    if _gmail_configured():
        scheduler.add_job(
            email_check,
            "interval",
            minutes=15,
            id="email_check",
            next_run_time=__import__("datetime").datetime.now(),
        )
        scheduler.add_job(daily_followup, "cron", hour=9, minute=5, id="daily_followup")
        logger.info(
            "Gmail configured — scheduling email_check every 15min, daily_followup at 9:05"
        )
    else:
        logger.warning(
            "Gmail not configured (credentials.json/token.json not found). "
            "Email classification and follow-up jobs are DISABLED. "
            "See SETUP.md for Gmail OAuth setup instructions."
        )

    scheduler.start()

    # Start resume upload service
    import uvicorn
    from local.resume_service import app as resume_app

    config = uvicorn.Config(resume_app, host="0.0.0.0", port=8001, log_level="info")
    server = uvicorn.Server(config)

    try:
        await server.serve()
    finally:
        scheduler.shutdown()
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
