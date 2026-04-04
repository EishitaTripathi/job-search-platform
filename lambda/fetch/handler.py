"""Lambda Fetch — private-fetch subnet (internet via NAT, no DB access).

Triggered by SQS. Fetches JD content from external URLs, stores in S3.
Deterministic: URL + ATS type → HTTP GET → S3. No reasoning = not an agent.

Supports three modes:
  1. URL mode (legacy): SQS message has {"url": "...", "job_id": "..."} — fetches single URL.
  2. Adapter mode: SQS message has {"source": "adzuna", "params": {...}} — uses pluggable adapter.
  3. Search mode: SQS message has {"job_id": "...", "company": "...", "role": "..."}
     — queries adapters to find a matching JD, then fetches and stores it.
"""

import dataclasses
import hashlib
import ipaddress
import json
import logging
import os
import socket
from urllib.parse import urlparse
from urllib.request import Request, HTTPRedirectHandler, build_opener
from urllib.error import URLError, HTTPError

import boto3

from adapter_registry import get_adapter

logger = logging.getLogger()
logger.setLevel(logging.INFO)

S3_BUCKET = os.environ["S3_BUCKET"]
s3 = boto3.client("s3")


class _SsrfSafeRedirectHandler(HTTPRedirectHandler):
    """Validate each redirect target against SSRF before following."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_safe_opener = build_opener(_SsrfSafeRedirectHandler)


def _validate_url(url: str) -> None:
    """Reject non-HTTP schemes and private/loopback/link-local IPs (SSRF protection)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Disallowed URL scheme: {parsed.scheme}")
    if not parsed.hostname:
        raise ValueError("URL has no hostname")
    for _, _, _, _, addr in socket.getaddrinfo(parsed.hostname, None):
        ip = ipaddress.ip_address(addr[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            raise ValueError(f"URL resolves to disallowed IP: {ip}")


def handler(event, context):
    """Process SQS messages — routes to adapter, URL, or search mode."""
    results = []

    for record in event.get("Records", []):
        try:
            body = json.loads(record["body"])
            source = body.get("source")

            if source:
                # Adapter mode: {"source": "adzuna", "params": {"query": "..."}}
                result = fetch_via_adapter(source, body.get("params", {}))
                results.append(result)
            elif body.get("url"):
                # URL mode (legacy): {"url": "https://...", "job_id": "..."}
                result = fetch_and_store(body["url"], body.get("job_id"))
                results.append(result)
            elif body.get("job_id") and body.get("company"):
                # Search mode: {"job_id": "...", "company": "...", "role": "..."}
                result = search_and_store(
                    body["job_id"],
                    body["company"],
                    body.get("role", ""),
                )
                results.append(result)
        except Exception as e:
            msg_id = record.get("messageId", "unknown")
            logger.error("Failed to process SQS record %s: %s", msg_id, e)
            results.append({"messageId": msg_id, "status": "failed", "error": str(e)})

    return {"statusCode": 200, "results": results}


# Adapters used for search-by-company when an email recommendation provides
# company+role but no direct URL. ATS adapters list all jobs for a company slug;
# _match_job() fuzzy-matches by role title. The Muse is a broad fallback.
SEARCH_ADAPTERS = ["greenhouse", "lever", "ashby", "the_muse"]


def _company_to_slug(company: str) -> str:
    """Convert company name to URL slug for ATS board APIs."""
    return company.lower().strip().replace(" ", "-").replace(".", "").replace(",", "")


def _match_job(jobs, company: str, role: str):
    """Find the best-matching NormalizedJob by company and role (case-insensitive substring)."""
    company_lower = company.lower()
    role_lower = role.lower()
    for job in jobs:
        if company_lower in job.company.lower() and role_lower in job.role.lower():
            return job
    # Fallback: match on company only
    for job in jobs:
        if company_lower in job.company.lower():
            return job
    return None


def search_and_store(job_id: str, company: str, role: str) -> dict:
    """Search ATS board APIs for a JD matching company+role, fetch and store it.

    Called when an email recommendation provides company+role but no direct URL.
    Tries each adapter in SEARCH_ADAPTERS order:
    - ATS adapters (greenhouse, lever, ashby): list all jobs for company slug, match by role
    - The Muse: broad keyword search, match by company+role
    """
    slug = _company_to_slug(company)

    for adapter_name in SEARCH_ADAPTERS:
        try:
            adapter = get_adapter(adapter_name)
            # ATS adapters need company slug; The Muse uses category params
            if adapter_name in ("greenhouse", "lever", "ashby"):
                params = {"company": slug}
            else:
                params = {"category": "Software Engineering"}
            jobs = adapter.fetch(params)
            match = _match_job(jobs, company, role)
            if match and match.ats_url:
                logger.info(
                    "Search hit for job_id=%s via %s: %s",
                    job_id,
                    adapter_name,
                    match.ats_url,
                )
                return fetch_and_store(match.ats_url, job_id)
        except Exception:
            logger.exception(
                "Search adapter %s failed for job_id=%s", adapter_name, job_id
            )
            continue

    logger.warning("No JD found for job_id=%s (%s — %s)", job_id, company, role)
    return {
        "job_id": job_id,
        "company": company,
        "role": role,
        "status": "no_match",
    }


def fetch_via_adapter(source: str, params: dict) -> dict:
    """Fetch jobs using a pluggable source adapter, store each in S3.

    Stores NormalizedJob JSON under jds/ to trigger Lambda Persist and KB indexing.
    Supports watermark filtering (since param) and S3 HeadObject dedup.
    """
    try:
        adapter = get_adapter(source)
        jobs = adapter.fetch(params)

        # Watermark filter: only keep jobs posted after the 'since' date
        since = params.get("since")
        if since:
            before = len(jobs)
            jobs = [j for j in jobs if j.date_posted and j.date_posted >= since]
            logger.info(
                "Watermark filter: %d -> %d jobs (since=%s)", before, len(jobs), since
            )

        jd_keys = []
        skipped = 0
        for job in jobs:
            job_dict = dataclasses.asdict(job)
            content = json.dumps(job_dict, default=str).encode("utf-8")
            content_hash = hashlib.sha256(content).hexdigest()

            # S3 dedup: skip if this content already exists
            jd_key = f"jds/{content_hash}.json"
            try:
                s3.head_object(Bucket=S3_BUCKET, Key=jd_key)
                skipped += 1
                continue  # Already stored — skip entirely
            except s3.exceptions.NoSuchKey:
                pass
            except Exception:
                pass  # head_object failed for other reason — proceed with put

            # Store structured metadata under jds/ (triggers Persist + KB indexing)
            s3.put_object(
                Bucket=S3_BUCKET,
                Key=jd_key,
                Body=content,
                ContentType="application/json",
            )
            jd_keys.append(jd_key)

        logger.info(
            "Adapter %s: %d fetched, %d new, %d skipped (dedup)",
            source,
            len(jobs),
            len(jd_keys),
            skipped,
        )
        return {
            "source": source,
            "jobs_fetched": len(jobs),
            "jd_keys": jd_keys,
            "skipped": skipped,
            "status": "success",
        }

    except ValueError as e:
        logger.error("Adapter error for source %s: %s", source, e)
        return {
            "source": source,
            "jobs_fetched": 0,
            "jd_keys": [],
            "skipped": 0,
            "status": "failed",
            "error": str(e),
        }
    except Exception as e:
        logger.error("Unexpected error fetching source %s: %s", source, e)
        return {
            "source": source,
            "jobs_fetched": 0,
            "jd_keys": [],
            "skipped": 0,
            "status": "failed",
            "error": "fetch_failed",
        }


def fetch_and_store(url: str, job_id: str | None) -> dict:
    """Fetch URL content and store in S3."""
    try:
        _validate_url(url)
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with _safe_opener.open(req, timeout=30) as resp:
            content = resp.read().decode("utf-8", errors="replace")

        content_hash = hashlib.sha256(content.encode()).hexdigest()
        s3_key = f"jds/{content_hash}.txt"

        s3.put_object(
            Bucket=S3_BUCKET,
            Key=s3_key,
            Body=content.encode("utf-8"),
            ContentType="text/plain",
        )

        return {
            "url": url,
            "job_id": job_id,
            "s3_key": s3_key,
            "content_hash": content_hash,
            "status": "success",
        }

    except ValueError as e:
        logger.error("URL validation failed for %s: %s", url, e)
        return {
            "url": url,
            "job_id": job_id,
            "s3_key": None,
            "content_hash": None,
            "status": "failed",
            "error": "url_validation_failed",
        }
    except (URLError, HTTPError) as e:
        logger.error("Fetch failed for %s: %s", url, e)
        return {
            "url": url,
            "job_id": job_id,
            "s3_key": None,
            "content_hash": None,
            "status": "failed",
            "error": "fetch_failed",
        }
