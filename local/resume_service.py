"""Resume upload service — localhost:8001.

Upload flow:
1. User uploads resume via local dashboard
2. Preview: Presidio strips PII, shows original vs redacted side-by-side
3. User edits redacted text (fix false positives)
4. Approve: stores final redacted version to filesystem + S3 + DB
5. Reject: discard and re-upload

PII never leaves the local machine.
"""

import asyncio
import os
import uuid
from pathlib import Path

try:
    import boto3
except ImportError:
    boto3 = None  # S3 upload optional — only needed in production

import magic
from fastapi import FastAPI, UploadFile, HTTPException, Depends, Header
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from local.agents.shared.redactor import PiiRedactor
from local.agents.shared.db import acquire

app = FastAPI(title="Resume Upload Service", version="0.2.0")
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
