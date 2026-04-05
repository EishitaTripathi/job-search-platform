"""FastAPI backend — CRUD endpoints + JWT auth.

Serves the dashboard and provides API endpoints for:
- Jobs (list, detail, update status)
- Labeling queue (list, resolve)
- Match reports (per resume)
- Follow-up recommendations
- Resumes (list)
- Orchestration runs (list)

Auth: JWT (HttpOnly, Secure, SameSite=Strict, 8h expiry)
Rate limiting: 5 attempts/minute on /login (slowapi)
"""

import asyncio
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import asyncpg
import boto3
import jwt
from fastapi import FastAPI, HTTPException, Depends, Response, Request, Cookie
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.middleware import SlowAPIMiddleware

from api.iam_auth import require_hmac_auth

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SQS client (lazy singleton, same pattern as bedrock_client.py)
# ---------------------------------------------------------------------------
_sqs_client = None
_sqs_queue_url: str | None = None
SQS_QUEUE_NAME = os.environ.get("SQS_QUEUE_NAME", "job-search-platform-jd-scrape-queue")
SQS_POLL_INTERVAL = int(os.environ.get("SQS_POLL_INTERVAL", "5"))
AWS_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-2")


def _get_sqs_client():
    global _sqs_client
    if _sqs_client is None:
        _sqs_client = boto3.client("sqs", region_name=AWS_REGION)
    return _sqs_client


def _get_queue_url() -> str:
    global _sqs_queue_url
    if _sqs_queue_url is None:
        _sqs_queue_url = _get_sqs_client().get_queue_url(QueueName=SQS_QUEUE_NAME)[
            "QueueUrl"
        ]
    return _sqs_queue_url


def _enqueue_jd_fetch(job_id: int, company: str, role: str) -> bool:
    """Enqueue a JD search request for JD Ingestion Agent. Returns True on success."""
    try:
        _get_sqs_client().send_message(
            QueueUrl=_get_queue_url(),
            MessageBody=json.dumps(
                {
                    "job_id": str(job_id),
                    "company": company,
                    "role": role,
                }
            ),
        )
        logger.info("Enqueued JD fetch for job_id=%d (%s — %s)", job_id, company, role)
        return True
    except Exception:
        logger.exception("Failed to enqueue JD fetch for job_id=%d", job_id)
        return False


JWT_SECRET = os.environ.get("JWT_SECRET")
if not JWT_SECRET:
    raise RuntimeError("JWT_SECRET environment variable is required")
JWT_EXPIRY_HOURS = 8
SECURE_COOKIES = os.environ.get("SECURE_COOKIES", "true").lower() == "true"
DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://jobsearch:localdev@localhost:5432/jobsearch",  # pragma: allowlist secret
)

_pool: asyncpg.Pool | None = None
_s3_client = None

ANALYSIS_POLL_INTERVAL = int(os.environ.get("ANALYSIS_POLL_INTERVAL", "60"))
S3_BUCKET_NAME = os.environ.get("S3_BUCKET", "")


def _get_s3_client():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3", region_name=AWS_REGION)
    return _s3_client


async def _poll_unanalyzed_jobs():
    """Background task: find jobs with analysis_status='pending', trigger Cloud Coordinator.

    Uses the analysis_status column instead of LEFT JOIN + in-memory failed set.
    Status transitions: pending → analyzing → completed|failed|skipped.
    Failed jobs stay 'failed' until manually reset — no infinite retry loops.
    """
    logger.info("Analysis polling started (interval=%ds)", ANALYSIS_POLL_INTERVAL)
    while True:
        try:
            await asyncio.sleep(ANALYSIS_POLL_INTERVAL)
            if _pool is None:
                continue

            # Fail fast if S3 bucket not configured
            if not S3_BUCKET_NAME:
                logger.error(
                    "Analysis polling: S3_BUCKET not configured, backing off 5 min"
                )
                await asyncio.sleep(300)
                continue

            async with _pool.acquire() as conn:
                # Check config flag
                flag = await conn.fetchval(
                    "SELECT value FROM config WHERE key = 'analysis_polling_enabled'"
                )
                if flag is not None and flag.lower() == "false":
                    continue

                # Advisory lock: only one ECS task polls at a time (prevents race during rolling deploy)
                locked = await conn.fetchval("SELECT pg_try_advisory_lock(42)")
                if not locked:
                    continue

                try:
                    # Find jobs pending analysis (excludes failed, completed, skipped, analyzing)
                    rows = await conn.fetch(
                        """
                        SELECT j.id, j.jd_s3_key FROM jobs j
                        WHERE j.jd_s3_key IS NOT NULL
                          AND j.analysis_status = 'pending'
                        ORDER BY j.created_at ASC LIMIT 5
                        """
                    )
                finally:
                    await conn.execute("SELECT pg_advisory_unlock(42)")

            if not rows:
                continue

            logger.info("Analysis polling: found %d pending jobs", len(rows))

            for row in rows:
                job_id = row["id"]
                s3_key = row["jd_s3_key"]
                try:
                    # Mark as analyzing (prevents re-pick on next cycle)
                    async with _pool.acquire() as conn:
                        await conn.execute(
                            "UPDATE jobs SET analysis_status = 'analyzing', "
                            "analysis_attempted_at = NOW() WHERE id = $1",
                            job_id,
                        )

                    # Read JD text from S3
                    obj = _get_s3_client().get_object(
                        Bucket=S3_BUCKET_NAME,
                        Key=s3_key,
                    )
                    jd_text = obj["Body"].read().decode("utf-8", errors="replace")

                    # If it's adapter JSON, extract description or use full content
                    if s3_key.endswith(".json"):
                        try:
                            data = json.loads(jd_text)
                            raw = data.get("raw_json") or {}
                            jd_text = (
                                raw.get("description", "")
                                if isinstance(raw, dict)
                                else jd_text
                            )
                            if not jd_text:
                                jd_text = json.dumps(data, default=str)
                        except json.JSONDecodeError:
                            pass

                    # Trigger Cloud Coordinator
                    from api.agents.cloud_coordinator.graph import run_cloud_coordinator

                    async with _pool.acquire() as conn:
                        await run_cloud_coordinator(
                            conn,
                            "new_jd",
                            {
                                "job_id": job_id,
                                "jd_text": jd_text,
                            },
                        )
                        # Mark completed
                        await conn.execute(
                            "UPDATE jobs SET analysis_status = 'completed' WHERE id = $1",
                            job_id,
                        )
                    logger.info("Analysis polling: completed job_id=%d", job_id)

                except RuntimeError as e:
                    if "not enabled" in str(e):
                        logger.error(
                            "Analysis polling: Bedrock model not enabled — "
                            "skipping remaining jobs this cycle. %s",
                            e,
                        )
                        # Mark this job as failed so it doesn't retry next cycle
                        async with _pool.acquire() as conn:
                            await conn.execute(
                                "UPDATE jobs SET analysis_status = 'failed', "
                                "analysis_error = $2 WHERE id = $1",
                                job_id,
                                str(e)[:500],
                            )
                        break
                    logger.exception("Analysis polling: failed for job_id=%d", job_id)
                    async with _pool.acquire() as conn:
                        await conn.execute(
                            "UPDATE jobs SET analysis_status = 'failed', "
                            "analysis_error = $2 WHERE id = $1",
                            job_id,
                            str(e)[:500],
                        )
                except Exception as e:
                    logger.exception("Analysis polling: failed for job_id=%d", job_id)
                    async with _pool.acquire() as conn:
                        await conn.execute(
                            "UPDATE jobs SET analysis_status = 'failed', "
                            "analysis_error = $2 WHERE id = $1",
                            job_id,
                            str(e)[:500],
                        )

        except asyncio.CancelledError:
            logger.info("Analysis polling stopped")
            return
        except Exception:
            logger.exception("Analysis polling: unexpected error")


async def _poll_sqs_messages():
    """Background task: consume SQS messages and run JD Ingestion Agent.

    Replaces Lambda Fetch + Lambda Persist. Screens sponsorship BEFORE S3 storage.
    Uses long-polling (WaitTimeSeconds=20 configured on queue) for efficiency.
    """
    if not SQS_QUEUE_NAME:
        logger.warning("SQS_QUEUE_NAME not configured, JD ingestion disabled")
        return

    logger.info("SQS consumer started (queue=%s)", SQS_QUEUE_NAME)

    from api.agents.jd_ingestion.graph import run_jd_ingestion
    from api.agents.jd_ingestion.tools import fetch_via_adapter

    sqs = boto3.client("sqs", region_name=AWS_REGION)

    try:
        queue_url_resp = await asyncio.to_thread(
            sqs.get_queue_url, QueueName=SQS_QUEUE_NAME
        )
        queue_url = queue_url_resp["QueueUrl"]
    except Exception:
        logger.exception("SQS consumer: failed to get queue URL for %s", SQS_QUEUE_NAME)
        return

    while True:
        try:
            if _pool is None:
                await asyncio.sleep(SQS_POLL_INTERVAL)
                continue

            # Check config flag
            async with _pool.acquire() as conn:
                flag = await conn.fetchval(
                    "SELECT value FROM config WHERE key = 'sqs_polling_enabled'"
                )
                if flag is not None and flag.lower() == "false":
                    await asyncio.sleep(SQS_POLL_INTERVAL)
                    continue

                # Advisory lock (separate from analysis poller's lock 42)
                locked = await conn.fetchval("SELECT pg_try_advisory_lock(43)")
                if not locked:
                    await asyncio.sleep(SQS_POLL_INTERVAL)
                    continue

            try:
                # Long-poll SQS (via thread to avoid blocking event loop for 20s)
                resp = await asyncio.to_thread(
                    sqs.receive_message,
                    QueueUrl=queue_url,
                    MaxNumberOfMessages=5,
                    WaitTimeSeconds=20,
                )

                messages = resp.get("Messages", [])
                if not messages:
                    continue

                logger.info("SQS consumer: received %d messages", len(messages))

                for msg in messages:
                    try:
                        body = json.loads(msg["Body"])
                        source = body.get("source")

                        if source:
                            # Adapter mode: fetch all jobs, then ingest each individually
                            jobs = await asyncio.to_thread(
                                fetch_via_adapter, source, body.get("params", {})
                            )
                            logger.info(
                                "SQS consumer: adapter %s returned %d jobs",
                                source,
                                len(jobs),
                            )

                            # Extend visibility for large batches (default 300s too short)
                            if len(jobs) > 50:
                                try:
                                    await asyncio.to_thread(
                                        sqs.change_message_visibility,
                                        QueueUrl=queue_url,
                                        ReceiptHandle=msg["ReceiptHandle"],
                                        VisibilityTimeout=3600,  # 1 hour
                                    )
                                except Exception:
                                    logger.warning(
                                        "SQS consumer: failed to extend visibility timeout"
                                    )

                            for job_data in jobs:
                                async with _pool.acquire() as conn:
                                    # Build a per-job message with the JD text pre-extracted
                                    raw = job_data.get("raw_json") or {}
                                    jd_text = (
                                        raw.get("description", "")
                                        if isinstance(raw, dict)
                                        else ""
                                    )
                                    per_job_body = {
                                        **body,
                                        "_job_data": job_data,
                                        "_jd_text": jd_text,
                                    }
                                    await run_jd_ingestion(conn, per_job_body)
                        else:
                            # URL or search mode: process single message
                            async with _pool.acquire() as conn:
                                await run_jd_ingestion(conn, body)

                        # Delete message on success
                        await asyncio.to_thread(
                            sqs.delete_message,
                            QueueUrl=queue_url,
                            ReceiptHandle=msg["ReceiptHandle"],
                        )

                    except Exception:
                        logger.exception(
                            "SQS consumer: failed to process message %s",
                            msg.get("MessageId", "unknown"),
                        )
                        # Don't delete — SQS will retry after visibility timeout

            finally:
                async with _pool.acquire() as conn:
                    await conn.execute("SELECT pg_advisory_unlock(43)")

        except asyncio.CancelledError:
            logger.info("SQS consumer stopped")
            return
        except Exception:
            logger.exception("SQS consumer: unexpected error")
            await asyncio.sleep(SQS_POLL_INTERVAL)


@asynccontextmanager
async def lifespan(app_instance):
    global _pool
    ssl = "require" if "rds.amazonaws.com" in DB_URL else None
    _pool = await asyncpg.create_pool(DB_URL, min_size=2, max_size=10, ssl=ssl)
    poll_task = asyncio.create_task(_poll_unanalyzed_jobs())
    sqs_task = asyncio.create_task(_poll_sqs_messages())
    yield
    poll_task.cancel()
    sqs_task.cancel()
    try:
        await poll_task
    except asyncio.CancelledError:
        pass
    try:
        await sqs_task
    except asyncio.CancelledError:
        pass
    if _pool:
        await _pool.close()


limiter = Limiter(key_func=get_remote_address)
app = FastAPI(
    title="Job Search Intelligence Platform", version="0.1.0", lifespan=lifespan
)
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)

# CORS for local dev
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:8001"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["*"],
)

# Serve dashboard static files
app.mount(
    "/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static"
)


@app.get("/")
async def root():
    """Redirect root to dashboard."""
    return RedirectResponse(url="/static/index.html")


@app.get("/health")
async def health():
    """ALB health check — no auth required."""
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    password: str


@app.post("/login")
@limiter.limit("5/minute")
async def login(request: Request, req: LoginRequest, response: Response):
    """Simple password-based auth. Single user, no registration needed."""
    expected = os.environ.get("APP_PASSWORD")
    if not expected:
        raise HTTPException(500, "APP_PASSWORD not configured")
    if not hmac.compare_digest(req.password.encode(), expected.encode()):
        raise HTTPException(401, "Invalid password")

    token = jwt.encode(
        {"exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRY_HOURS)},
        JWT_SECRET,
        algorithm="HS256",
    )
    response.set_cookie(
        key="token",
        value=token,
        httponly=True,
        secure=SECURE_COOKIES,
        samesite="strict",
        max_age=JWT_EXPIRY_HOURS * 3600,
    )
    return {"status": "ok"}


async def require_auth(token: Optional[str] = Cookie(None)):
    """Dependency: verify JWT from HttpOnly cookie."""
    if not token:
        raise HTTPException(401, "Not authenticated")
    try:
        jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid token")


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

POSTED_AFTER_MAP = {"1d": "1 day", "7d": "7 days", "14d": "14 days", "30d": "30 days"}


@app.get("/api/jobs")
async def list_jobs(
    sort: str = "date",
    resume_id: Optional[int] = None,
    status: Optional[str] = None,
    posted_after: Optional[str] = None,
    _auth=Depends(require_auth),
):
    """List jobs — reverse chronological or by best match per resume.

    Supports date range filtering (posted_after: 1d, 7d, 14d, 30d) and
    company/title blocklist from config table.
    """
    interval = POSTED_AFTER_MAP.get(posted_after or "")
    async with _pool.acquire() as conn:
        # Load blocklists from config
        bl_companies = (
            await conn.fetchval(
                "SELECT value FROM config WHERE key = 'blocklist_companies'"
            )
            or ""
        )
        bl_titles = (
            await conn.fetchval(
                "SELECT value FROM config WHERE key = 'blocklist_titles'"
            )
            or ""
        )

        if sort == "match" and resume_id:
            rows = await conn.fetch(
                """
                SELECT j.*, mr.overall_fit_score, mr.fit_category, mr.reasoning
                FROM jobs j
                LEFT JOIN match_reports mr ON mr.job_id = j.id AND mr.resume_id = $1
                WHERE ($2::text IS NULL OR j.status = $2)
                  AND ($3::text = '' OR j.company NOT IN (
                      SELECT trim(unnest(string_to_array($3, ',')))))
                  AND ($4::text = '' OR NOT EXISTS (
                      SELECT 1 FROM unnest(string_to_array($4, ',')) AS blocked
                      WHERE lower(j.role) LIKE '%' || lower(trim(blocked)) || '%'))
                  AND ($5::text IS NULL OR j.date_posted >= NOW() - $5::interval)
                ORDER BY mr.overall_fit_score DESC NULLS LAST, j.date_posted DESC
                """,
                resume_id,
                status,
                bl_companies,
                bl_titles,
                interval,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT j.*
                FROM jobs j
                WHERE ($1::text IS NULL OR j.status = $1)
                  AND ($2::text = '' OR j.company NOT IN (
                      SELECT trim(unnest(string_to_array($2, ',')))))
                  AND ($3::text = '' OR NOT EXISTS (
                      SELECT 1 FROM unnest(string_to_array($3, ',')) AS blocked
                      WHERE lower(j.role) LIKE '%' || lower(trim(blocked)) || '%'))
                  AND ($4::text IS NULL OR j.date_posted >= NOW() - $4::interval)
                ORDER BY j.date_posted DESC NULLS LAST, j.created_at DESC
                """,
                status,
                bl_companies,
                bl_titles,
                interval,
            )
    return [dict(r) for r in rows]


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: int, _auth=Depends(require_auth)):
    """Job detail: JD analysis, match reports, application timeline."""
    async with _pool.acquire() as conn:
        job = await conn.fetchrow("SELECT * FROM jobs WHERE id = $1", job_id)
        if not job:
            raise HTTPException(404, "Job not found")

        analysis = await conn.fetchrow(
            "SELECT * FROM jd_analyses WHERE job_id = $1", job_id
        )
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
        followups = await conn.fetch(
            "SELECT * FROM followup_recommendations WHERE job_id = $1 ORDER BY created_at DESC",
            job_id,
        )

    return {
        "job": dict(job),
        "analysis": dict(analysis) if analysis else None,
        "match_reports": [dict(m) for m in matches],
        "followups": [dict(f) for f in followups],
    }


class JobStatusUpdate(BaseModel):
    status: str


class IngestStatus(BaseModel):
    job_id: int
    stage: str
    deadline: Optional[str] = None  # YYYY-MM-DD


class IngestRecommendation(BaseModel):
    company: str
    role: str


class IngestFollowup(BaseModel):
    job_id: int
    urgency: str
    action: str


class IngestResume(BaseModel):
    name: str
    s3_key: str


class ChatRequest(BaseModel):
    job_id: int
    question: str


@app.patch("/api/jobs/{job_id}")
async def update_job(job_id: int, req: JobStatusUpdate, _auth=Depends(require_auth)):
    """Update job status."""
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE jobs SET status = $1, last_updated = NOW() WHERE id = $2",
            req.status,
            job_id,
        )
    return {"status": "updated"}


# ---------------------------------------------------------------------------
# Follow-up Recommendations
# ---------------------------------------------------------------------------


@app.get("/api/followups")
async def list_followups(
    urgency: Optional[str] = None,
    _auth=Depends(require_auth),
):
    """List follow-up recommendations with optional urgency filter."""
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT fr.*, j.company, j.role, j.status AS job_status
            FROM followup_recommendations fr
            JOIN jobs j ON j.id = fr.job_id
            WHERE fr.acted_on = FALSE
              AND ($1::text IS NULL OR fr.urgency_level = $1)
            ORDER BY
                CASE fr.urgency_level
                    WHEN 'high' THEN 1
                    WHEN 'medium' THEN 2
                    WHEN 'low' THEN 3
                END,
                fr.created_at DESC
            """,
            urgency,
        )
    return [dict(r) for r in rows]


@app.post("/api/followups/{followup_id}/act")
async def mark_acted(followup_id: int, _auth=Depends(require_auth)):
    """Mark a follow-up recommendation as acted on."""
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE followup_recommendations SET acted_on = TRUE WHERE id = $1",
            followup_id,
        )
    return {"status": "acted"}


# ---------------------------------------------------------------------------
# Resumes
# ---------------------------------------------------------------------------


@app.get("/api/resumes")
async def list_resumes(_auth=Depends(require_auth)):
    """List all uploaded resumes."""
    async with _pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM resumes ORDER BY uploaded_at DESC")
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Orchestration Runs (debugging)
# ---------------------------------------------------------------------------


@app.get("/api/runs")
async def list_runs(limit: int = 20, _auth=Depends(require_auth)):
    """List recent orchestration runs."""
    limit = min(max(limit, 1), 100)
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM orchestration_runs ORDER BY started_at DESC LIMIT $1",
            limit,
        )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Ingestion API (HMAC-authenticated — from local pipeline)
# ---------------------------------------------------------------------------


@app.post("/api/ingest/status")
async def ingest_status(req: IngestStatus, _auth=Depends(require_hmac_auth)):
    """Receive validated status update from local pipeline."""
    async with _pool.acquire() as conn:
        # Verify job exists
        job = await conn.fetchrow("SELECT id FROM jobs WHERE id = $1", req.job_id)
        if not job:
            raise HTTPException(404, "Job not found")

        # Update job status
        await conn.execute(
            "UPDATE jobs SET status = $1, last_updated = NOW() WHERE id = $2",
            req.stage,
            req.job_id,
        )

        # Insert deadline if provided
        if req.deadline:
            await conn.execute(
                """
                INSERT INTO deadlines (job_id, deadline_text, deadline_date)
                VALUES ($1, $2, $3::date)
                """,
                req.job_id,
                req.deadline,
                req.deadline,
            )

    return {"status": "ingested", "type": "status"}


@app.post("/api/ingest/recommendation")
async def ingest_recommendation(
    req: IngestRecommendation, _auth=Depends(require_hmac_auth)
):
    """Receive validated job recommendation from local pipeline."""
    async with _pool.acquire() as conn:
        # Create job record (dedup on company+role+source)
        row = await conn.fetchrow(
            """
            INSERT INTO jobs (company, role, source, status)
            VALUES ($1, $2, 'email_recommendation', 'to_apply')
            ON CONFLICT (company, role, source) DO NOTHING
            RETURNING id
            """,
            req.company,
            req.role,
        )
        job_id = row["id"] if row else None

    # Enqueue for JD fetch via SQS (JD Ingestion Agent will search for the JD)
    if job_id is not None:
        _enqueue_jd_fetch(job_id, req.company, req.role)

    return {"status": "ingested", "type": "recommendation", "job_id": job_id}


@app.post("/api/ingest/followup")
async def ingest_followup(req: IngestFollowup, _auth=Depends(require_hmac_auth)):
    """Receive validated follow-up action from local pipeline."""
    async with _pool.acquire() as conn:
        job = await conn.fetchrow("SELECT id FROM jobs WHERE id = $1", req.job_id)
        if not job:
            raise HTTPException(404, "Job not found")

        await conn.execute(
            """
            INSERT INTO followup_recommendations (job_id, urgency_level, recommended_action, urgency_reasoning, acted_on)
            VALUES ($1, $2, $3, $4, FALSE)
            """,
            req.job_id,
            req.urgency,
            req.action,
            f"Auto-generated: {req.urgency} urgency",
        )

    return {"status": "ingested", "type": "followup"}


@app.post("/api/ingest/resume")
async def ingest_resume(req: IngestResume, _auth=Depends(require_hmac_auth)):
    """Receive validated resume record from local pipeline."""
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO resumes (name, s3_key)
            VALUES ($1, $2)
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            req.name,
            req.s3_key,
        )
        resume_id = row["id"] if row else None

    return {"status": "ingested", "type": "resume", "resume_id": resume_id}


@app.delete("/api/ingest/resume/{s3_key:path}")
async def delete_resume_record(s3_key: str, _auth=Depends(require_hmac_auth)):
    """Delete a resume record from cloud RDS by s3_key."""
    async with _pool.acquire() as conn:
        await conn.execute("DELETE FROM resumes WHERE s3_key = $1", s3_key)
    return {"status": "deleted"}


# ---------------------------------------------------------------------------
# Chat (JWT-authenticated — from dashboard)
# ---------------------------------------------------------------------------


@app.post("/api/chat")
async def chat(req: ChatRequest, _auth=Depends(require_auth)):
    """Chat with Application Chat agent about a specific job."""
    async with _pool.acquire() as conn:
        job = await conn.fetchrow(
            "SELECT company, role FROM jobs WHERE id = $1", req.job_id
        )
        if not job:
            raise HTTPException(404, "Job not found")

        try:
            from api.agents.cloud_coordinator.graph import run_cloud_coordinator

            result = await run_cloud_coordinator(
                conn,
                "chat",
                {
                    "job_id": req.job_id,
                    "question": req.question,
                },
            )
            answer = (
                result.get("results", {}).get("application_chat", {}).get("answer", "")
            )
            if not answer:
                answer = (
                    "The chat agent could not generate an answer. Please try again."
                )
        except Exception as exc:
            logger.exception("Chat agent failed for job_id=%d", req.job_id)
            answer = f"Chat agent error: {str(exc)}"

    return {"answer": answer, "job_id": req.job_id}


# ---------------------------------------------------------------------------
# Blocklist (JWT-authenticated — from dashboard)
# ---------------------------------------------------------------------------


class BlocklistUpdate(BaseModel):
    companies: str = ""
    titles: str = ""


@app.get("/api/blocklist")
async def get_blocklist(_auth=Depends(require_auth)):
    """Get current company/title blocklist."""
    async with _pool.acquire() as conn:
        companies = (
            await conn.fetchval(
                "SELECT value FROM config WHERE key = 'blocklist_companies'"
            )
            or ""
        )
        titles = (
            await conn.fetchval(
                "SELECT value FROM config WHERE key = 'blocklist_titles'"
            )
            or ""
        )
    return {"companies": companies, "titles": titles}


@app.post("/api/blocklist")
async def update_blocklist(req: BlocklistUpdate, _auth=Depends(require_auth)):
    """Update company/title blocklist."""
    async with _pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO config (key, value) VALUES ('blocklist_companies', $1)
            ON CONFLICT (key) DO UPDATE SET value = $1, updated_at = NOW()
            """,
            req.companies,
        )
        await conn.execute(
            """
            INSERT INTO config (key, value) VALUES ('blocklist_titles', $1)
            ON CONFLICT (key) DO UPDATE SET value = $1, updated_at = NOW()
            """,
            req.titles,
        )
    return {"status": "updated"}


# ---------------------------------------------------------------------------
# Ops & Deadlines (JWT-authenticated — from dashboard)
# ---------------------------------------------------------------------------


@app.get("/api/deadlines")
async def list_deadlines(_auth=Depends(require_auth)):
    """List upcoming deadlines joined with job info."""
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT d.*, j.company, j.role, j.status AS job_status
            FROM deadlines d
            JOIN jobs j ON j.id = d.job_id
            WHERE d.deadline_date >= CURRENT_DATE
            ORDER BY d.deadline_date ASC
            """,
        )
    return [dict(r) for r in rows]


@app.get("/api/ops/metrics")
async def ops_metrics(_auth=Depends(require_auth)):
    """Aggregated pipeline metrics."""
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT source, metric_name, COUNT(*) as count,
                   MAX(recorded_at) as last_recorded
            FROM pipeline_metrics
            GROUP BY source, metric_name
            ORDER BY source, metric_name
            """,
        )
    return [dict(r) for r in rows]


@app.post("/api/admin/reset")
async def admin_reset(_auth=Depends(require_auth)):
    """Reset all pipeline data. Truncates jobs, analyses, match reports, and runs.

    Auth-protected (JWT). For development/testing — clears all transient data
    while preserving resumes and config.
    """
    async with _pool.acquire() as conn:
        await conn.execute(
            """
            TRUNCATE match_reports, jd_analyses, followup_recommendations,
                     orchestration_runs, pipeline_metrics, deadlines,
                     answer_memory
            CASCADE
            """
        )
        deleted = await conn.fetchval(
            "WITH d AS (DELETE FROM jobs RETURNING 1) SELECT count(*) FROM d"
        )
    logger.info("Admin reset: deleted %d jobs and all related data", deleted or 0)
    return {"status": "reset", "jobs_deleted": deleted or 0}
