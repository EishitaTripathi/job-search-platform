"""HMAC-SHA256 authentication for ingestion endpoints.

Local pipeline signs payloads with shared secret (INGEST_HMAC_KEY from Secrets Manager).
Cloud API verifies signature before processing.
"""

import hashlib
import hmac
import os
import time

from fastapi import HTTPException, Request

INGEST_HMAC_KEY = os.environ.get("INGEST_HMAC_KEY", "")
MAX_TIMESTAMP_DRIFT = 300  # 5 minutes


async def require_hmac_auth(request: Request):
    """Dependency: verify HMAC signature from local pipeline."""
    if not INGEST_HMAC_KEY:
        raise HTTPException(500, "INGEST_HMAC_KEY not configured")

    signature = request.headers.get("X-Signature")
    timestamp = request.headers.get("X-Timestamp")

    if not signature or not timestamp:
        raise HTTPException(401, "Missing authentication headers")

    # Check timestamp drift (replay protection)
    try:
        ts = int(timestamp)
    except ValueError:
        raise HTTPException(401, "Invalid timestamp")

    if abs(time.time() - ts) > MAX_TIMESTAMP_DRIFT:
        raise HTTPException(401, "Request expired")

    # Read body and verify signature
    body = await request.body()
    message = f"{timestamp}.{body.decode()}"
    expected = hmac.new(
        INGEST_HMAC_KEY.encode(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(signature, expected):
        raise HTTPException(401, "Invalid signature")
