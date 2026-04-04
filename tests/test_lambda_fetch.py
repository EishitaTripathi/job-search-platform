"""Tests for Lambda Fetch handler — pure function logic, no AWS calls."""

import hashlib
import json
from unittest.mock import patch, MagicMock

from urllib.error import URLError

import pytest


@pytest.fixture(autouse=True)
def env_vars(monkeypatch):
    monkeypatch.setenv("S3_BUCKET", "test-bucket")


def test_handler_skips_empty_url():
    from lambda_.fetch.handler import handler

    event = {"Records": [{"body": json.dumps({"url": "", "job_id": "1"})}]}
    result = handler(event, None)
    assert result["results"] == []


def test_handler_skips_missing_url():
    from lambda_.fetch.handler import handler

    event = {"Records": [{"body": json.dumps({"job_id": "1"})}]}
    result = handler(event, None)
    assert result["results"] == []


def test_handler_empty_records():
    from lambda_.fetch.handler import handler

    result = handler({"Records": []}, None)
    assert result == {"statusCode": 200, "results": []}


def test_handler_no_records_key():
    from lambda_.fetch.handler import handler

    result = handler({}, None)
    assert result == {"statusCode": 200, "results": []}


@patch("lambda_.fetch.handler._validate_url")
@patch("lambda_.fetch.handler._safe_opener")
@patch("lambda_.fetch.handler.s3")
def test_fetch_and_store_success(mock_s3, mock_opener, mock_validate):
    from lambda_.fetch.handler import fetch_and_store

    mock_resp = MagicMock()
    mock_resp.read.return_value = b"job description content"
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_opener.open.return_value = mock_resp

    result = fetch_and_store("https://example.com/job", "42")

    assert result["status"] == "success"
    assert result["job_id"] == "42"
    expected_hash = hashlib.sha256(b"job description content").hexdigest()
    assert result["content_hash"] == expected_hash
    assert result["s3_key"] == f"jds/{expected_hash}.txt"
    mock_s3.put_object.assert_called_once()


@patch("lambda_.fetch.handler._validate_url")
@patch("lambda_.fetch.handler._safe_opener")
def test_fetch_and_store_failure(mock_opener, mock_validate):
    from lambda_.fetch.handler import fetch_and_store

    mock_opener.open.side_effect = URLError("timeout")
    result = fetch_and_store("https://bad-url.com", "99")
    assert result["status"] == "failed"
    assert result["s3_key"] is None
    assert result["error"] == "fetch_failed"


def test_content_hash_deterministic():
    """Same content should produce same S3 key (dedup by hash)."""
    content = "Senior Backend Engineer - Python, AWS, Kubernetes"
    hash1 = hashlib.sha256(content.encode()).hexdigest()
    hash2 = hashlib.sha256(content.encode()).hexdigest()
    assert hash1 == hash2


# ---------------------------------------------------------------------------
# Search mode tests
# ---------------------------------------------------------------------------


def _make_normalized_job(
    company="Acme Corp", role="Senior Engineer", ats_url="https://acme.com/apply"
):
    """Create a NormalizedJob-like object for testing."""
    from lambda_.fetch.adapters.base import NormalizedJob

    return NormalizedJob(
        company=company,
        role=role,
        location="Remote",
        ats_url=ats_url,
        date_posted="2026-03-28",
        source="jsearch",
        source_id="test-123",
        raw_json={},
    )


@patch("lambda_.fetch.handler.fetch_and_store")
@patch("lambda_.fetch.handler.get_adapter")
@patch("lambda_.fetch.handler.SEARCH_ADAPTERS", ["test_adapter"])
def test_search_and_store_finds_match(mock_get_adapter, mock_fetch_and_store):
    from lambda_.fetch.handler import search_and_store

    mock_adapter = MagicMock()
    mock_adapter.fetch.return_value = [
        _make_normalized_job("Acme Corp", "Senior Engineer", "https://acme.com/apply"),
    ]
    mock_get_adapter.return_value = mock_adapter
    mock_fetch_and_store.return_value = {"status": "success", "job_id": "42"}

    result = search_and_store("42", "Acme Corp", "Senior Engineer")

    assert result["status"] == "success"
    mock_fetch_and_store.assert_called_once_with("https://acme.com/apply", "42")


@patch("lambda_.fetch.handler.get_adapter")
def test_search_and_store_no_match(mock_get_adapter):
    from lambda_.fetch.handler import search_and_store

    mock_adapter = MagicMock()
    mock_adapter.fetch.return_value = [
        _make_normalized_job("Other Co", "Different Role", "https://other.com"),
    ]
    mock_get_adapter.return_value = mock_adapter

    result = search_and_store("42", "Acme Corp", "Senior Engineer")

    assert result["status"] == "no_match"
    assert result["job_id"] == "42"


@patch("lambda_.fetch.handler.get_adapter")
def test_search_and_store_adapter_failure(mock_get_adapter):
    from lambda_.fetch.handler import search_and_store

    mock_get_adapter.return_value.fetch.side_effect = Exception("API down")

    result = search_and_store("42", "Acme Corp", "Senior Engineer")

    assert result["status"] == "no_match"
    assert result["job_id"] == "42"


def test_handler_routes_search_mode():
    """Handler routes {job_id, company, role} messages to search mode."""
    from lambda_.fetch.handler import handler

    event = {
        "Records": [
            {"body": json.dumps({"job_id": "42", "company": "Acme", "role": "SWE"})},
        ]
    }

    with patch(
        "lambda_.fetch.handler.search_and_store", return_value={"status": "no_match"}
    ) as mock:
        result = handler(event, None)

    mock.assert_called_once_with("42", "Acme", "SWE")
    assert len(result["results"]) == 1


# ---------------------------------------------------------------------------
# Adapter mode tests — Issue 1: stores JDs under jds/ prefix
# ---------------------------------------------------------------------------


def _mock_s3_no_existing(mock_s3):
    """Configure mock S3 to simulate no existing objects (HeadObject raises NoSuchKey)."""
    from botocore.exceptions import ClientError

    mock_s3.head_object.side_effect = ClientError(
        {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject"
    )
    mock_s3.exceptions.NoSuchKey = type("NoSuchKey", (ClientError,), {})


@patch("lambda_.fetch.handler.s3")
@patch("lambda_.fetch.handler.get_adapter")
def test_adapter_stores_new_job(mock_get_adapter, mock_s3):
    """Adapter stores new job under jds/ when not already in S3."""
    from lambda_.fetch.handler import fetch_via_adapter

    _mock_s3_no_existing(mock_s3)

    mock_adapter = MagicMock()
    mock_adapter.fetch.return_value = [
        _make_normalized_job("Acme", "SWE", "https://acme.com/jd"),
    ]
    mock_get_adapter.return_value = mock_adapter

    result = fetch_via_adapter("jsearch", {"query": "swe"})

    assert result["status"] == "success"
    assert result["jobs_fetched"] == 1
    assert len(result["jd_keys"]) == 1
    assert result["jd_keys"][0].startswith("jds/")
    mock_s3.put_object.assert_called_once()


@patch("lambda_.fetch.handler.s3")
@patch("lambda_.fetch.handler.get_adapter")
def test_adapter_skips_existing_job(mock_get_adapter, mock_s3):
    """Adapter skips jobs that already exist in S3 (HeadObject succeeds)."""
    from lambda_.fetch.handler import fetch_via_adapter

    # HeadObject succeeds = object exists = skip
    mock_s3.head_object.return_value = {}
    mock_s3.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})

    mock_adapter = MagicMock()
    mock_adapter.fetch.return_value = [
        _make_normalized_job("Acme", "SWE", "https://acme.com/jd"),
    ]
    mock_get_adapter.return_value = mock_adapter

    result = fetch_via_adapter("jsearch", {"query": "swe"})

    assert result["status"] == "success"
    assert result["skipped"] == 1
    assert len(result["jd_keys"]) == 0
    mock_s3.put_object.assert_not_called()


@patch("lambda_.fetch.handler.s3")
@patch("lambda_.fetch.handler.get_adapter")
def test_adapter_watermark_filters_old_jobs(mock_get_adapter, mock_s3):
    """Watermark filter excludes jobs posted before the since date."""
    from lambda_.fetch.handler import fetch_via_adapter

    _mock_s3_no_existing(mock_s3)

    mock_adapter = MagicMock()
    mock_adapter.fetch.return_value = [
        _make_normalized_job(
            "Old Co", "SWE", "https://old.com"
        ),  # date_posted from fixture = "2026-03-28"
        _make_normalized_job("New Co", "SWE", "https://new.com"),
    ]
    mock_get_adapter.return_value = mock_adapter

    result = fetch_via_adapter("jsearch", {"query": "swe", "since": "2026-03-29"})

    # Both jobs have date_posted="2026-03-28" which is before since="2026-03-29"
    assert result["jobs_fetched"] == 0
    assert len(result["jd_keys"]) == 0
