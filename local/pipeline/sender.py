"""Send validated payloads to cloud ingestion API with HMAC authentication."""

import asyncio
import hashlib
import hmac
import logging
import os
import time

import httpx

logger = logging.getLogger(__name__)

CLOUD_API_URL = os.environ.get("CLOUD_API_URL", "")
INGEST_HMAC_KEY = os.environ.get("INGEST_HMAC_KEY", "")


def _sign_payload(payload_bytes: bytes, timestamp: str) -> str:
    """Generate HMAC-SHA256 signature for payload."""
    message = f"{timestamp}.{payload_bytes.decode()}"
    return hmac.new(
        INGEST_HMAC_KEY.encode(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()


async def send_to_cloud(endpoint: str, payload) -> bool:
    """Send a validated payload to cloud ingestion API.

    Args:
        endpoint: One of 'status', 'recommendation', 'followup'
        payload: A validated Pydantic model instance

    Returns:
        True if successfully sent, False otherwise
    """
    if not CLOUD_API_URL or not INGEST_HMAC_KEY:
        logger.warning("CLOUD_API_URL or INGEST_HMAC_KEY not configured, skipping send")
        return False

    url = f"{CLOUD_API_URL}/api/ingest/{endpoint}"
    payload_bytes = payload.model_dump_json().encode()
    timestamp = str(int(time.time()))
    signature = _sign_payload(payload_bytes, timestamp)

    headers = {
        "Content-Type": "application/json",
        "X-Signature": signature,
        "X-Timestamp": timestamp,
    }

    # Retry with exponential backoff
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(url, content=payload_bytes, headers=headers)
                if resp.status_code == 200:
                    logger.info("Sent %s payload to cloud", endpoint)
                    return True
                logger.warning(
                    "Cloud returned %d for %s: %s",
                    resp.status_code,
                    endpoint,
                    resp.text,
                )
        except httpx.HTTPError as e:
            logger.warning("Attempt %d failed for %s: %s", attempt + 1, endpoint, e)

        if attempt < 2:
            await asyncio.sleep(2**attempt)  # 1s, 2s backoff

    logger.error("Failed to send %s payload after 3 attempts", endpoint)
    return False


async def send_to_cloud_with_response(endpoint: str, payload) -> dict | None:
    """Send a validated payload and return the JSON response, or None on failure."""
    if not CLOUD_API_URL or not INGEST_HMAC_KEY:
        logger.warning("CLOUD_API_URL or INGEST_HMAC_KEY not configured, skipping send")
        return None

    url = f"{CLOUD_API_URL}/api/ingest/{endpoint}"
    payload_bytes = payload.model_dump_json().encode()
    timestamp = str(int(time.time()))
    signature = _sign_payload(payload_bytes, timestamp)

    headers = {
        "Content-Type": "application/json",
        "X-Signature": signature,
        "X-Timestamp": timestamp,
    }

    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(url, content=payload_bytes, headers=headers)
                if resp.status_code == 200:
                    logger.info("Sent %s payload to cloud", endpoint)
                    return resp.json()
                logger.warning(
                    "Cloud returned %d for %s: %s",
                    resp.status_code,
                    endpoint,
                    resp.text,
                )
        except httpx.HTTPError as e:
            logger.warning("Attempt %d failed for %s: %s", attempt + 1, endpoint, e)

        if attempt < 2:
            await asyncio.sleep(2**attempt)

    logger.error("Failed to send %s payload after 3 attempts", endpoint)
    return None


async def send_to_cloud_delete(endpoint: str, resource_id: str) -> bool:
    """Send a DELETE request to cloud ingestion API.

    Args:
        endpoint: Resource type (e.g. 'resume')
        resource_id: Resource identifier (e.g. s3_key)

    Returns:
        True if successfully deleted, False otherwise.
    """
    if not CLOUD_API_URL or not INGEST_HMAC_KEY:
        logger.warning(
            "CLOUD_API_URL or INGEST_HMAC_KEY not configured, skipping delete"
        )
        return False

    url = f"{CLOUD_API_URL}/api/ingest/{endpoint}/{resource_id}"
    timestamp = str(int(time.time()))
    signature = _sign_payload(resource_id.encode(), timestamp)

    headers = {
        "X-Signature": signature,
        "X-Timestamp": timestamp,
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.delete(url, headers=headers)
            if resp.status_code == 200:
                logger.info("Deleted %s/%s from cloud", endpoint, resource_id)
                return True
            logger.warning(
                "Cloud returned %d for DELETE %s: %s",
                resp.status_code,
                endpoint,
                resp.text,
            )
    except httpx.HTTPError as e:
        logger.warning("Failed to delete %s from cloud: %s", endpoint, e)

    return False
