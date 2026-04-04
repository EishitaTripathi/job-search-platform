"""JD Ingestion Agent tools — fetch, screen, store, persist, analyze.

Consolidates logic from Lambda Fetch + Lambda Persist + Sponsorship Screener
into a single ECS-based pipeline that screens BEFORE storing to S3.
"""

import dataclasses
import hashlib
import ipaddress
import json
import logging
import os
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

import boto3
from botocore.exceptions import ClientError

from api.agents.jd_ingestion.adapter_registry import SEARCH_ADAPTERS, get_adapter

logger = logging.getLogger(__name__)

S3_BUCKET = os.environ.get("S3_BUCKET", "")
AWS_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-2")


# ---------------------------------------------------------------------------
# SSRF protection (from Lambda Fetch)
# ---------------------------------------------------------------------------


class _SsrfSafeRedirectHandler(HTTPRedirectHandler):
    """Validate each redirect target against SSRF before following."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_safe_opener = build_opener(_SsrfSafeRedirectHandler)


def _validate_url(url: str) -> None:
    """Reject non-HTTP schemes and private/loopback/link-local IPs."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Disallowed URL scheme: {parsed.scheme}")
    if not parsed.hostname:
        raise ValueError("URL has no hostname")
    for _, _, _, _, addr in socket.getaddrinfo(parsed.hostname, None):
        ip = ipaddress.ip_address(addr[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            raise ValueError(f"URL resolves to disallowed IP: {ip}")


# ---------------------------------------------------------------------------
# Fetch strategy
# ---------------------------------------------------------------------------


def determine_fetch_strategy(body: dict) -> str:
    """Determine fetch mode from SQS message body."""
    if body.get("source"):
        return "adapter"
    elif body.get("url"):
        return "url"
    elif body.get("job_id") and body.get("company"):
        return "search"
    else:
        raise ValueError(f"Cannot determine fetch strategy from message: {body}")


# ---------------------------------------------------------------------------
# Adapter fetch (from Lambda Fetch fetch_via_adapter)
# ---------------------------------------------------------------------------


def fetch_via_adapter(source: str, params: dict) -> list[dict]:
    """Call a source adapter and return NormalizedJob dicts (no S3 storage)."""
    adapter = get_adapter(source)
    jobs = adapter.fetch(params)
    return [dataclasses.asdict(j) for j in jobs]


# ---------------------------------------------------------------------------
# URL fetch (from Lambda Fetch fetch_and_store)
# ---------------------------------------------------------------------------


def fetch_url_content(url: str) -> str:
    """Fetch JD content from a URL with SSRF validation. Returns text content."""
    _validate_url(url)
    req = Request(url, headers={"User-Agent": "JobSearchPlatform/1.0"})
    try:
        resp = _safe_opener.open(req, timeout=30)
        return resp.read().decode("utf-8", errors="replace")
    except (HTTPError, URLError) as e:
        logger.error("URL fetch failed for %s: %s", url, e)
        raise


# ---------------------------------------------------------------------------
# Search (from Lambda Fetch search_and_store)
# ---------------------------------------------------------------------------


def _company_to_slug(company: str) -> str:
    """Convert company name to URL slug for ATS board APIs."""
    return company.lower().strip().replace(" ", "-").replace(".", "").replace(",", "")


def _match_job(jobs, company: str, role: str):
    """Find best-matching NormalizedJob by company+role (case-insensitive substring)."""
    company_lower = company.lower()
    role_lower = role.lower()
    for job in jobs:
        c = job.company if hasattr(job, "company") else job.get("company", "")
        r = job.role if hasattr(job, "role") else job.get("role", "")
        if company_lower in c.lower() and role_lower in r.lower():
            return job
    # Fallback: match on company only
    for job in jobs:
        c = job.company if hasattr(job, "company") else job.get("company", "")
        if company_lower in c.lower():
            return job
    return None


def search_for_jd(company: str, role: str) -> dict | None:
    """Search ATS board APIs for a JD matching company+role. Returns NormalizedJob dict or None."""
    slug = _company_to_slug(company)
    for adapter_name in SEARCH_ADAPTERS:
        try:
            adapter = get_adapter(adapter_name)
            if adapter_name in ("greenhouse", "lever", "ashby"):
                params = {"company": slug}
            else:
                params = {"category": "Software Engineering"}
            jobs = adapter.fetch(params)
            match = _match_job(jobs, company, role)
            if match:
                url = (
                    match.ats_url
                    if hasattr(match, "ats_url")
                    else match.get("ats_url", "")
                )
                if url:
                    logger.info("Search hit via %s: %s", adapter_name, url)
                    return (
                        dataclasses.asdict(match)
                        if hasattr(match, "ats_url")
                        else match
                    )
        except Exception:
            logger.exception(
                "Search adapter %s failed for %s — %s", adapter_name, company, role
            )
            continue
    return None


# ---------------------------------------------------------------------------
# Sponsorship screening (delegates to existing screener tools)
# ---------------------------------------------------------------------------


async def screen_sponsorship(jd_text: str) -> dict:
    """Screen JD for sponsorship exclusion signals using Haiku.

    Returns dict with sponsorship_status and reasoning.
    Reuses the existing Sponsorship Screener LLM logic.
    """
    from api.agents.sponsorship_screener.tools import analyze_sponsorship

    return await analyze_sponsorship(jd_text)


# ---------------------------------------------------------------------------
# S3 storage (from Lambda Fetch, with HeadObject dedup)
# ---------------------------------------------------------------------------

MAX_CONTENT_SIZE = 1_048_576  # 1MB DoS protection


def _get_s3_client():
    return boto3.client("s3", region_name=AWS_REGION)


def content_hash(content: str) -> str:
    """SHA-256 hash for content-addressable S3 storage."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def store_to_s3(jd_text: str, job_data: dict | None = None) -> str | None:
    """Store JD to S3 under jds/ prefix. Returns s3_key or None if duplicate.

    If job_data is provided (adapter mode), stores as JSON with metadata.
    Otherwise stores as plain text.
    """
    s3 = _get_s3_client()
    bucket = S3_BUCKET
    if not bucket:
        logger.warning("S3_BUCKET not configured, skipping S3 storage")
        return None

    # Content size guard (DoS prevention — matches persist_to_rds check)
    if len(jd_text.encode("utf-8")) > MAX_CONTENT_SIZE:
        logger.warning(
            "JD content exceeds %d bytes, skipping S3 storage", MAX_CONTENT_SIZE
        )
        return None

    h = content_hash(jd_text)

    if job_data:
        s3_key = f"jds/{h}.json"
        body = json.dumps(job_data, default=str).encode("utf-8")
    else:
        s3_key = f"jds/{h}.txt"
        body = jd_text.encode("utf-8")

    # HeadObject dedup
    try:
        s3.head_object(Bucket=bucket, Key=s3_key)
        logger.info("S3 dedup: %s already exists, skipping", s3_key)
        return s3_key  # Already exists
    except ClientError as e:
        if e.response["Error"]["Code"] == "404":
            pass  # Doesn't exist, proceed to upload
        else:
            raise

    s3.put_object(Bucket=bucket, Key=s3_key, Body=body)
    logger.info("Stored JD to s3://%s/%s", bucket, s3_key)
    return s3_key


# ---------------------------------------------------------------------------
# RDS persist (from Lambda Persist, converted psycopg2 → asyncpg)
# ---------------------------------------------------------------------------


async def persist_to_rds(
    conn,
    company: str,
    role: str,
    source: str,
    s3_key: str,
    ats_url: str | None = None,
    raw_json: dict | None = None,
    date_posted: str | None = None,
) -> int | None:
    """Upsert job record to RDS. Returns job_id or None on conflict.

    Uses ON CONFLICT (jd_s3_key) DO NOTHING for dedup.
    Falls back to ats_url lookup if s3_key conflict.
    """
    row = await conn.fetchrow(
        """
        INSERT INTO jobs (company, role, source, jd_s3_key, ats_url, raw_json, date_posted, analysis_status)
        VALUES ($1, $2, $3, $4, $5, $6, $7::date, 'pending')
        ON CONFLICT (jd_s3_key) DO NOTHING
        RETURNING id
        """,
        company,
        role,
        source,
        s3_key,
        ats_url,
        json.dumps(raw_json) if raw_json else None,
        date_posted,
    )
    if row:
        return row["id"]

    # Fallback: check if ats_url already exists
    if ats_url:
        existing = await conn.fetchrow(
            "SELECT id FROM jobs WHERE ats_url = $1", ats_url
        )
        if existing:
            return existing["id"]

    return None
