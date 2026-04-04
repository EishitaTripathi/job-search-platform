"""Lambda Persist — private-data subnet (RDS + S3, no internet).

Triggered by S3 event (new JD text uploaded). Reads from S3, writes to RDS.
Deterministic: S3 event → read → RDS write. No reasoning = not an agent.

Uses psycopg2 (sync) — Lambda convention per CLAUDE.md.
"""

import json
import logging
import os

import boto3
import psycopg2
import psycopg2.errors

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Defer client creation to first invocation (allows test import without AWS config)
s3 = None
sm = None
_secret_cache = {}


def _ensure_clients():
    global s3, sm
    if s3 is None:
        s3 = boto3.client("s3")
    if sm is None:
        sm = boto3.client("secretsmanager")


def _get_secret():
    _ensure_clients()
    if not _secret_cache:
        resp = sm.get_secret_value(SecretId=os.environ["SECRET_NAME"])
        _secret_cache.update(json.loads(resp["SecretString"]))
    return _secret_cache


def get_db_connection():
    """Create a psycopg2 connection to RDS. SSL required."""
    secret = _get_secret()
    return psycopg2.connect(
        host=secret["DB_HOST"],
        port=5432,
        dbname=secret["DB_NAME"],
        user=secret["DB_USER"],
        password=secret["DB_PASSWORD"],
        sslmode="require",
    )


def handler(event, context):
    """Process S3 event notifications for new JD text files."""
    results = []

    for record in event.get("Records", []):
        s3_key = record["s3"]["object"]["key"]
        bucket = record["s3"]["bucket"]["name"]

        if not s3_key.startswith("jds/"):
            continue

        result = read_and_persist(bucket, s3_key)
        results.append(result)

    return {"statusCode": 200, "results": results}


MAX_CONTENT_SIZE = 1_048_576  # 1 MB — reject oversized objects to prevent DoS


def _parse_adapter_json(
    content: str, s3_key: str
) -> tuple[str, str, str, str, str | None]:
    """Parse adapter JSON to extract company, role, source, raw_json, date_posted.

    Returns (company, role, source, raw_json_str, date_posted).
    """
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        logger.warning("Invalid JSON in %s, treating as unknown", s3_key)
        return ("Unknown", "Unknown", "fetch", json.dumps({"s3_key": s3_key}), None)

    date_posted = data.get("date_posted")
    # Handle Unix timestamps (Simplify uses epoch seconds)
    if isinstance(date_posted, (int, float)) and date_posted > 1_000_000_000:
        from datetime import datetime, timezone

        date_posted = datetime.fromtimestamp(date_posted, tz=timezone.utc).strftime(
            "%Y-%m-%d"
        )

    return (
        data.get("company") or "Unknown",
        data.get("role") or "Unknown",
        data.get("source") or "fetch",
        content,
        date_posted if isinstance(date_posted, str) else None,
    )


def read_and_persist(bucket: str, s3_key: str) -> dict:
    """Read JD content from S3 and create/update job record in RDS.

    Handles two formats:
    - jds/*.txt — raw JD text from URL fetch (company/role extracted later by JD Analyzer)
    - jds/*.json — structured NormalizedJob from adapter fetch (company/role available)
    """
    _ensure_clients()
    try:
        obj = s3.get_object(Bucket=bucket, Key=s3_key)
        content_bytes = obj["Body"].read()

        if len(content_bytes) > MAX_CONTENT_SIZE:
            logger.error(
                "S3 object %s exceeds %d bytes, skipping", s3_key, MAX_CONTENT_SIZE
            )
            return {
                "s3_key": s3_key,
                "job_id": None,
                "status": "skipped",
                "error": "content_too_large",
            }

        content = content_bytes.decode("utf-8")
        filename = s3_key.split("/")[-1]

        # Determine format from file extension
        date_posted = None
        if filename.endswith(".json"):
            company, role, source, raw_json, date_posted = _parse_adapter_json(
                content, s3_key
            )
        else:
            content_hash = filename.replace(".txt", "")
            company, role, source = "Unknown", "Unknown", "fetch"
            raw_json = json.dumps({"content_hash": content_hash})

        # Also extract ats_url from adapter data if present
        ats_url = None
        if filename.endswith(".json"):
            try:
                ats_url = json.loads(content).get("ats_url") or None
            except (json.JSONDecodeError, AttributeError):
                pass

        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                # Upsert job with S3 key — dedup on jd_s3_key.
                # ats_url is also UNIQUE; if it conflicts separately, catch
                # the IntegrityError and look up the existing job instead.
                try:
                    cur.execute(
                        """
                        INSERT INTO jobs (company, role, source, jd_s3_key, ats_url, raw_json, date_posted)
                        VALUES (%s, %s, %s, %s, %s, %s, %s::date)
                        ON CONFLICT (jd_s3_key) DO NOTHING
                        RETURNING id
                        """,
                        (company, role, source, s3_key, ats_url, raw_json, date_posted),
                    )
                    row = cur.fetchone()
                    job_id = row[0] if row else None
                except psycopg2.errors.UniqueViolation:
                    conn.rollback()
                    logger.info(
                        "Duplicate ats_url for %s, looking up existing job", s3_key
                    )
                    cur.execute("SELECT id FROM jobs WHERE ats_url = %s", (ats_url,))
                    row = cur.fetchone()
                    job_id = row[0] if row else None
                else:
                    conn.commit()

            return {
                "s3_key": s3_key,
                "job_id": job_id,
                "status": "success",
            }
        finally:
            conn.close()

    except Exception as e:
        logger.error("Failed to persist %s: %s", s3_key, e)
        return {
            "s3_key": s3_key,
            "job_id": None,
            "status": "failed",
            "error": "internal_error",
        }
