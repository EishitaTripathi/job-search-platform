"""Local dashboard — localhost:8001.

Features:
1. Resume upload with PII redaction preview
2. Labeling queue review (email classification correction)
3. Gmail status and model accuracy tracking

PII never leaves the local machine.
"""

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:
    import boto3
except ImportError:
    boto3 = None  # S3 upload optional — only needed in production

import magic
from fastapi import FastAPI, UploadFile, HTTPException, Depends, Header, Query
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from local.agents.shared.redactor import PiiRedactor
from local.agents.shared.db import acquire
from local.agents.email_classifier.tools import store_labeled_example

logger = logging.getLogger(__name__)

app = FastAPI(title="Local Dashboard", version="0.3.0")
redactor = PiiRedactor()

RESUME_STORAGE_PATH = os.environ.get("RESUME_STORAGE_PATH", "/tmp/resumes")
RESUME_API_KEY = os.environ.get("RESUME_API_KEY")
S3_BUCKET = os.environ.get("S3_BUCKET", "")
AWS_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-2")
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
ALLOWED_MIMES = {
    "application/pdf",
    "text/plain",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

_s3_client = None


def _get_s3():
    global _s3_client
    if _s3_client is None and S3_BUCKET and boto3:
        _s3_client = boto3.client("s3", region_name=AWS_REGION)
    return _s3_client


async def _require_api_key(x_api_key: str = Header(None)):
    """Verify API key for resume service endpoints."""
    if RESUME_API_KEY and x_api_key != RESUME_API_KEY:
        raise HTTPException(401, "Invalid or missing API key")


@app.on_event("startup")
async def startup():
    os.makedirs(RESUME_STORAGE_PATH, exist_ok=True)


# Serve local dashboard
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.post("/upload/preview")
async def preview_resume(
    file: UploadFile,
    _auth=Depends(_require_api_key),
):
    """Upload a resume, strip PII, return original + redacted for review.

    Does NOT store anything — this is a preview step.
    """
    if not file.filename or not file.filename.endswith((".pdf", ".txt", ".docx")):
        raise HTTPException(400, "Supported formats: .pdf, .txt, .docx")

    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(
            413, f"File too large. Max: {MAX_FILE_SIZE // (1024 * 1024)} MB"
        )

    mime = magic.from_buffer(content[:2048], mime=True)
    if mime not in ALLOWED_MIMES:
        raise HTTPException(400, f"Invalid file type: {mime}")

    # Extract text based on file type
    if mime == "application/pdf":
        import io
        import pdfplumber

        text = ""
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"
        if not text.strip():
            raise HTTPException(
                400, "Could not extract text from PDF. Try a text-based PDF."
            )
    else:
        text = content.decode("utf-8", errors="replace")

    # Run Presidio to get both redacted text and entity details
    results = redactor._analyzer.analyze(
        text=text,
        entities=redactor.ENTITIES,
        language="en",
    )
    redacted_text = redactor.redact(text)

    entities = []
    for r in results:
        entities.append(
            {
                "type": r.entity_type,
                "text": text[r.start : r.end],
                "score": round(r.score, 2),
                "start": r.start,
                "end": r.end,
            }
        )

    return {
        "original": text,
        "redacted": redacted_text,
        "entities": entities,
        "filename": file.filename,
    }


class ApproveRequest(BaseModel):
    name: str
    redacted_text: str


@app.post("/upload/approve")
async def approve_resume(
    req: ApproveRequest,
    _auth=Depends(_require_api_key),
):
    """Store the approved (user-edited) redacted resume."""
    resume_id = str(uuid.uuid4())
    filename = f"{resume_id}.txt"
    filepath = os.path.join(RESUME_STORAGE_PATH, filename)

    # Store locally
    with open(filepath, "w") as f:
        f.write(req.redacted_text)

    s3_key = f"resumes/{filename}"

    # Upload to S3 if configured (via thread to avoid blocking event loop)
    s3 = _get_s3()
    if s3 and S3_BUCKET:
        await asyncio.to_thread(
            s3.put_object,
            Bucket=S3_BUCKET,
            Key=s3_key,
            Body=req.redacted_text.encode("utf-8"),
            ContentType="text/plain",
        )

    # Create database record (local DB)
    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO resumes (name, s3_key)
            VALUES ($1, $2)
            RETURNING id, name, s3_key, uploaded_at
            """,
            req.name,
            s3_key,
        )

    # Send resume record to cloud RDS via validated pipeline
    try:
        from local.pipeline.sender import send_to_cloud
        from pydantic import BaseModel as _BM

        class _ResumePayload(_BM):
            name: str
            s3_key: str

        await send_to_cloud("resume", _ResumePayload(name=req.name, s3_key=s3_key))
    except Exception:
        import logging

        logging.getLogger(__name__).warning(
            "Failed to sync resume to cloud — will retry later"
        )

    return {
        "id": row["id"],
        "name": row["name"],
        "s3_key": row["s3_key"],
        "uploaded_at": row["uploaded_at"].isoformat(),
    }


@app.get("/resumes/{resume_id}/text")
async def get_resume_text(resume_id: int, _auth=Depends(_require_api_key)):
    """Return the redacted text of a stored resume."""
    async with acquire() as conn:
        row = await conn.fetchrow(
            "SELECT s3_key FROM resumes WHERE id = $1",
            resume_id,
        )
    if not row:
        raise HTTPException(404, "Resume not found")

    # Try local filesystem first
    filename = row["s3_key"].split("/")[-1]
    filepath = os.path.join(RESUME_STORAGE_PATH, filename)
    if os.path.exists(filepath):
        with open(filepath) as f:
            return {"text": f.read()}

    # Fall back to S3 (via thread to avoid blocking event loop)
    s3 = _get_s3()
    if s3 and S3_BUCKET:
        obj = await asyncio.to_thread(
            s3.get_object, Bucket=S3_BUCKET, Key=row["s3_key"]
        )
        return {"text": obj["Body"].read().decode("utf-8")}

    raise HTTPException(404, "Resume file not found")


# Legacy upload endpoint (kept for backward compatibility)
@app.post("/upload")
async def upload_resume(
    file: UploadFile,
    name: str = "Default Resume",
    _auth=Depends(_require_api_key),
):
    """Upload a resume, strip PII, store redacted version (no preview)."""
    if not file.filename or not file.filename.endswith((".pdf", ".txt", ".docx")):
        raise HTTPException(400, "Supported formats: .pdf, .txt, .docx")

    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(
            413, f"File too large. Max: {MAX_FILE_SIZE // (1024 * 1024)} MB"
        )

    mime = magic.from_buffer(content[:2048], mime=True)
    if mime not in ALLOWED_MIMES:
        raise HTTPException(400, f"Invalid file type: {mime}")

    if mime == "application/pdf":
        import io
        import pdfplumber

        text = ""
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"
    else:
        text = content.decode("utf-8", errors="replace")
    redacted_text = redactor.redact(text)

    resume_id = str(uuid.uuid4())
    filename = f"{resume_id}.txt"
    filepath = os.path.join(RESUME_STORAGE_PATH, filename)
    with open(filepath, "w") as f:
        f.write(redacted_text)

    s3_key = f"resumes/{filename}"

    async with acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO resumes (name, s3_key) VALUES ($1, $2) RETURNING id, name, s3_key, uploaded_at",
            name,
            s3_key,
        )

    return {
        "id": row["id"],
        "name": row["name"],
        "s3_key": row["s3_key"],
        "uploaded_at": row["uploaded_at"].isoformat(),
    }


@app.delete("/resumes/{resume_id}")
async def delete_resume(resume_id: int, _auth=Depends(_require_api_key)):
    """Delete a resume from DB, local filesystem, and S3."""
    async with acquire() as conn:
        row = await conn.fetchrow("SELECT s3_key FROM resumes WHERE id = $1", resume_id)
        if not row:
            raise HTTPException(404, "Resume not found")
        # Delete from DB (cascades to match_reports)
        await conn.execute("DELETE FROM resumes WHERE id = $1", resume_id)

    # Delete local file
    filename = row["s3_key"].split("/")[-1]
    filepath = os.path.join(RESUME_STORAGE_PATH, filename)
    if os.path.exists(filepath):
        os.remove(filepath)

    # Delete from S3 (via thread to avoid blocking event loop)
    s3 = _get_s3()
    if s3 and S3_BUCKET:
        try:
            await asyncio.to_thread(
                s3.delete_object, Bucket=S3_BUCKET, Key=row["s3_key"]
            )
        except Exception:
            pass  # Best effort — file may not exist in S3

    # Delete from cloud RDS
    try:
        from local.pipeline.sender import send_to_cloud_delete

        await send_to_cloud_delete("resume", row["s3_key"])
    except Exception:
        import logging

        logging.getLogger(__name__).warning("Failed to delete resume from cloud RDS")

    return {"status": "deleted"}


@app.get("/resumes")
async def list_resumes(_auth=Depends(_require_api_key)):
    """List all uploaded resumes."""
    async with acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, name, s3_key, uploaded_at FROM resumes ORDER BY uploaded_at DESC"
        )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Labeling Queue Review
# ---------------------------------------------------------------------------

VALID_QUEUE_LABELS = {
    "irrelevant",
    "status_update",
    "recommendation",
    "to_apply",
    "waiting_for_referral",
    "applied",
    "assessment",
    "assignment",
    "interview",
    "offer",
    "rejected",
}


@app.get("/api/queue")
async def list_queue(_auth=Depends(_require_api_key)):
    """List unresolved items in the labeling queue."""
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, email_id, subject, snippet, guessed_stage,
                   guessed_company, guessed_role, created_at
            FROM labeling_queue
            WHERE resolved = FALSE
            ORDER BY created_at DESC
            """
        )
    return [dict(r) for r in rows]


class QueueResolveRequest(BaseModel):
    label: str


@app.post("/api/queue/{queue_id}/resolve")
async def resolve_queue_item(
    queue_id: int, req: QueueResolveRequest, _auth=Depends(_require_api_key)
):
    """Resolve a labeling queue item with user-confirmed label.

    Stores the label in ChromaDB (for RAG few-shot) and labeled_emails table,
    then marks the queue item resolved.
    """
    if req.label not in VALID_QUEUE_LABELS:
        raise HTTPException(
            400, f"Invalid label '{req.label}'. Valid: {sorted(VALID_QUEUE_LABELS)}"
        )

    async with acquire() as conn:
        item = await conn.fetchrow(
            "SELECT id, email_id, subject, snippet FROM labeling_queue WHERE id = $1 AND resolved = FALSE",
            queue_id,
        )
    if not item:
        raise HTTPException(404, "Queue item not found or already resolved")

    # Store in ChromaDB + labeled_emails (generates embedding)
    await store_labeled_example(
        email_id=item["email_id"],
        subject=item["subject"],
        snippet=item["snippet"],
        label=req.label,
        confirmed_by="user",
    )

    # Only mark resolved after store succeeds
    async with acquire() as conn:
        await conn.execute(
            "UPDATE labeling_queue SET resolved = TRUE WHERE id = $1", queue_id
        )

    return {"status": "resolved"}


@app.get("/api/classifications")
async def list_classifications(_auth=Depends(_require_api_key)):
    """List all classified emails (auto + user) for review and re-labeling."""
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, email_id, subject, snippet, stage, confirmed_by, created_at
            FROM labeled_emails
            ORDER BY created_at DESC
            """
        )
    return [dict(r) for r in rows]


class RelabelRequest(BaseModel):
    label: str


STAGE_LABELS = {"applied", "assessment", "assignment", "interview", "offer", "rejected"}


@app.post("/api/classifications/{classification_id}/relabel")
async def relabel_classification(
    classification_id: int, req: RelabelRequest, _auth=Depends(_require_api_key)
):
    """Re-label a classification. Updates DB, ChromaDB, and re-routes through pipeline."""
    if req.label not in VALID_QUEUE_LABELS:
        raise HTTPException(
            400,
            f"Invalid label '{req.label}'. Valid: {sorted(VALID_QUEUE_LABELS)}",
        )

    # Join labeling_queue to get body (needed for pipeline re-routing)
    async with acquire() as conn:
        item = await conn.fetchrow(
            """
            SELECT le.id, le.email_id, le.subject, le.snippet, le.stage AS current_stage,
                   lq.body
            FROM labeled_emails le
            LEFT JOIN labeling_queue lq ON lq.email_id = le.email_id
            WHERE le.id = $1
            """,
            classification_id,
        )
    if not item:
        raise HTTPException(404, "Classification not found")

    # Re-label always sets confirmed_by='user' and sends to cloud.
    # The "Reviewed" button uses confirmAsIs() which sets 'verified' instead.
    new_confirmed_by = "user"

    # Update DB
    async with acquire() as conn:
        await conn.execute(
            "UPDATE labeled_emails SET stage = $1, confirmed_by = $2 WHERE id = $3",
            req.label,
            new_confirmed_by,
            classification_id,
        )

    # Update ChromaDB for RAG few-shot
    try:
        from local.agents.shared.embedder import LocalEmbedder
        from local.agents.shared.memory import get_email_collection

        embedder = LocalEmbedder()
        email_text = f"{item['subject']} {item['snippet']}"
        embedding = embedder.embed(email_text)
        collection = get_email_collection()
        collection.upsert(
            ids=[item["email_id"]],
            documents=[email_text],
            embeddings=[embedding],
            metadatas=[{"label": req.label, "confirmed_by": new_confirmed_by}],
        )
    except Exception:
        logger.warning("Failed to update ChromaDB for relabel")

    # Send user's label directly to cloud (don't re-run classifier)
    routed = False
    if req.label != "irrelevant":
        try:
            from local.pipeline.sender import send_to_cloud, send_to_cloud_with_response
            from local.pipeline.schemas import StatusPayload, RecommendationPayload

            if req.label in STAGE_LABELS or req.label == "status_update":
                # Extract company/role for job creation
                company = None
                role = None
                try:
                    from local.agents.shared.llm import (
                        llm_generate,
                        sanitize_for_prompt,
                    )
                    import re as _re

                    extract_prompt = (
                        "Extract the company name and job role from this email. "
                        'Respond with ONLY JSON: {"company": "name", "role": "title"}\n\n'
                        f"Subject: {sanitize_for_prompt(item['subject'])}\n"
                        f"Snippet: {sanitize_for_prompt(item['snippet'] or '')}"
                    )
                    resp_text = await llm_generate(
                        extract_prompt, temperature=0.0, max_tokens=100
                    )
                    match = _re.search(r"\{[^}]+\}", resp_text)
                    if match:
                        extracted = json.loads(match.group())
                        company = extracted.get("company")
                        role = extracted.get("role")
                except Exception:
                    logger.warning("Failed to extract company/role for relabel")

                # Create job in cloud via recommendation
                job_id = None
                if company and role:
                    rec_resp = await send_to_cloud_with_response(
                        "recommendation",
                        RecommendationPayload(company=company, role=role),
                    )
                    if rec_resp and rec_resp.get("job_id"):
                        job_id = rec_resp["job_id"]
                        # Create locally too
                        async with acquire() as conn:
                            await conn.execute(
                                """INSERT INTO jobs (id, company, role, source, status)
                                   VALUES ($1, $2, $3, 'email_recommendation', $4)
                                   ON CONFLICT DO NOTHING""",
                                job_id,
                                company,
                                role,
                                req.label,
                            )

                # Send status with user's chosen label directly
                if job_id:
                    # Extract deadline if stage warrants it
                    deadline = None
                    body = item["body"] or ""
                    if req.label in ("assessment", "assignment", "interview") and body:
                        try:
                            deadline_prompt = (
                                "Extract any deadline date from this email. "
                                "Respond with ONLY a JSON object: "
                                '{"deadline": "YYYY-MM-DD"} or {"deadline": null}\n\n'
                                f"Email: {sanitize_for_prompt(body[:2000])}"
                            )
                            dl_resp = await llm_generate(
                                deadline_prompt, temperature=0.0, max_tokens=50
                            )
                            dl_match = _re.search(r"\{[^}]+\}", dl_resp)
                            if dl_match:
                                dl_data = json.loads(dl_match.group())
                                if dl_data.get("deadline"):
                                    deadline = dl_data["deadline"]
                        except Exception:
                            logger.warning("Failed to extract deadline")

                    payload = StatusPayload(
                        job_id=job_id, stage=req.label, deadline=deadline
                    )
                    await send_to_cloud("status", payload)
                    routed = True

                    # Check if email requires immediate user action
                    if req.label in ("assessment", "assignment", "interview") and body:
                        try:
                            from local.pipeline.schemas import FollowupPayload

                            action_prompt = (
                                "Does this email require the recipient to take action "
                                "(reply to schedule, click a link, complete an assessment)?\n"
                                'Respond with ONLY JSON: {"needs_action": true} or {"needs_action": false}\n\n'
                                f"Subject: {sanitize_for_prompt(item['subject'])}\n"
                                f"Email: {sanitize_for_prompt(body[:2000])}"
                            )
                            action_resp = await llm_generate(
                                action_prompt, temperature=0.0, max_tokens=50
                            )
                            action_match = _re.search(r"\{[^}]+\}", action_resp)
                            if action_match:
                                action_data = json.loads(action_match.group())
                                if action_data.get("needs_action"):
                                    followup = FollowupPayload(
                                        job_id=job_id,
                                        urgency="high",
                                        action="send_followup",
                                    )
                                    await send_to_cloud("followup", followup)
                        except Exception:
                            logger.warning(
                                "Failed to check action-required for relabel"
                            )

            elif req.label == "recommendation":
                # Extract and send recommendation
                try:
                    from local.agents.shared.llm import (
                        llm_generate,
                        sanitize_for_prompt,
                    )
                    import re as _re

                    extract_prompt = (
                        "Extract the company name and job role from this email. "
                        'Respond with ONLY JSON: {"company": "name", "role": "title"}\n\n'
                        f"Subject: {sanitize_for_prompt(item['subject'])}\n"
                        f"Snippet: {sanitize_for_prompt(item['snippet'] or '')}"
                    )
                    resp_text = await llm_generate(
                        extract_prompt, temperature=0.0, max_tokens=100
                    )
                    match = _re.search(r"\{[^}]+\}", resp_text)
                    if match:
                        extracted = json.loads(match.group())
                        company = extracted.get("company")
                        role = extracted.get("role")
                        if company and role:
                            await send_to_cloud_with_response(
                                "recommendation",
                                RecommendationPayload(company=company, role=role),
                            )
                            routed = True
                except Exception:
                    logger.warning("Failed to extract/send recommendation for relabel")

        except Exception:
            logger.exception("Pipeline re-routing failed for relabel")

    return {"status": "relabeled", "routed": routed}


@app.get("/api/queue/metrics")
async def queue_metrics(_auth=Depends(_require_api_key)):
    """Classification accuracy summary."""
    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT COUNT(*) FILTER (WHERE confirmed_by IN ('auto', 'verified')) AS auto_classified,
                   COUNT(*) FILTER (WHERE confirmed_by = 'user') AS user_corrected
            FROM labeled_emails
            """
        )
        queue_depth = await conn.fetchval(
            "SELECT COUNT(*) FROM labeling_queue WHERE resolved = FALSE"
        )

    auto = row["auto_classified"] or 0
    user = row["user_corrected"] or 0
    total = auto + user

    return {
        "total_classified": total,
        "auto_classified": auto,
        "user_corrected": user,
        "accuracy_rate": round(auto / total, 3) if total > 0 else None,
        "queue_depth": queue_depth or 0,
    }


@app.get("/api/queue/metrics/history")
async def queue_metrics_history(_auth=Depends(_require_api_key)):
    """Daily accuracy trend over the last 30 days."""
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT created_at::date AS date,
                   COUNT(*) FILTER (WHERE confirmed_by IN ('auto', 'verified')) AS auto_count,
                   COUNT(*) FILTER (WHERE confirmed_by = 'user') AS user_count
            FROM labeled_emails
            GROUP BY created_at::date
            ORDER BY date DESC
            LIMIT 30
            """
        )

    history = []
    for r in rows:
        auto = r["auto_count"] or 0
        user = r["user_count"] or 0
        total = auto + user
        history.append(
            {
                "date": r["date"].isoformat(),
                "auto": auto,
                "user": user,
                "accuracy": round(auto / total, 3) if total > 0 else None,
            }
        )

    return {"history": history}


# ---------------------------------------------------------------------------
# Gmail Status
# ---------------------------------------------------------------------------

GMAIL_CREDENTIALS_PATH = os.environ.get(
    "GMAIL_CREDENTIALS_PATH", "credentials/credentials.json"
)
GMAIL_TOKEN_PATH = os.environ.get("GMAIL_TOKEN_PATH", "credentials/token.json")


@app.get("/api/gmail/status")
async def gmail_status(_auth=Depends(_require_api_key)):
    """Gmail credential and token status + last email check info."""
    creds_exist = os.path.exists(GMAIL_CREDENTIALS_PATH)
    token_exist = os.path.exists(GMAIL_TOKEN_PATH)

    result = {
        "credentials_exist": creds_exist,
        "token_exist": token_exist,
        "token_has_refresh": False,
        "token_expired": None,
        "token_expiry": None,
        "last_email_check": None,
    }

    # Parse token.json if it exists
    if token_exist:
        try:
            with open(GMAIL_TOKEN_PATH) as f:
                token_data = json.load(f)
            result["token_has_refresh"] = bool(token_data.get("refresh_token"))
            expiry = token_data.get("expiry")
            if expiry:
                result["token_expiry"] = expiry
                try:
                    exp_dt = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
                    result["token_expired"] = exp_dt < datetime.now(timezone.utc)
                except (ValueError, TypeError):
                    pass
        except (json.JSONDecodeError, OSError):
            pass

    # Last email_check orchestration run
    try:
        async with acquire() as conn:
            run = await conn.fetchrow(
                """
                SELECT run_id, status, started_at, completed_at, agent_results, error
                FROM orchestration_runs
                WHERE event_type = 'email_check'
                ORDER BY started_at DESC LIMIT 1
                """
            )
        if run:
            agent_results = run["agent_results"] or {}
            result["last_email_check"] = {
                "run_id": str(run["run_id"]),
                "status": run["status"],
                "started_at": run["started_at"].isoformat()
                if run["started_at"]
                else None,
                "completed_at": run["completed_at"].isoformat()
                if run["completed_at"]
                else None,
                "emails_fetched": agent_results.get("emails_fetched"),
                "emails_processed": agent_results.get("emails_processed"),
            }
    except Exception:
        logger.warning("Failed to fetch last email_check run")

    return result


# ---------------------------------------------------------------------------
# Orchestration Runs
# ---------------------------------------------------------------------------


@app.get("/api/runs")
async def list_runs(
    limit: int = Query(default=20, le=100), _auth=Depends(_require_api_key)
):
    """Recent orchestration runs."""
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT run_id, event_type, agent_chain, status, error,
                   started_at, completed_at,
                   EXTRACT(EPOCH FROM (completed_at - started_at)) AS duration_seconds
            FROM orchestration_runs
            ORDER BY started_at DESC
            LIMIT $1
            """,
            limit,
        )
    return [dict(r) for r in rows]
