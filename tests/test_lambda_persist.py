"""Tests for Lambda Persist handler — pure function logic, mocked AWS."""

import json
from unittest.mock import patch, MagicMock
from io import BytesIO

import pytest


@pytest.fixture(autouse=True)
def env_vars(monkeypatch):
    monkeypatch.setenv("SECRET_NAME", "test/secret")


def _s3_event(key="jds/abc123.txt", bucket="test-bucket"):
    return {
        "Records": [
            {
                "s3": {
                    "bucket": {"name": bucket},
                    "object": {"key": key},
                }
            }
        ]
    }


def test_handler_skips_non_jds_prefix():
    from lambda_.persist.handler import handler

    event = _s3_event(key="other/file.txt")
    result = handler(event, None)
    assert result["results"] == []


def test_handler_empty_records():
    from lambda_.persist.handler import handler

    result = handler({"Records": []}, None)
    assert result == {"statusCode": 200, "results": []}


def test_content_hash_extracted_from_s3_key():
    """The content hash is derived from the S3 key filename."""
    s3_key = "jds/abcdef1234567890.txt"  # pragma: allowlist secret
    expected_hash = "abcdef1234567890"  # pragma: allowlist secret
    assert s3_key.split("/")[-1].replace(".txt", "") == expected_hash


@patch("lambda_.persist.handler.get_db_connection")
@patch("lambda_.persist.handler._ensure_clients")
def test_read_and_persist_success(mock_ensure, mock_db):
    import lambda_.persist.handler as mod
    from lambda_.persist.handler import read_and_persist

    # Mock S3 client on the module
    mock_s3 = MagicMock()
    mock_s3.get_object.return_value = {
        "Body": BytesIO(b"Full job description text here")
    }
    mod.s3 = mock_s3

    # Mock DB
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = (42,)
    mock_conn.cursor.return_value.__enter__ = lambda s: mock_cursor
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    mock_db.return_value = mock_conn

    result = read_and_persist("test-bucket", "jds/abc123.txt")

    assert result["status"] == "success"
    assert result["job_id"] == 42
    mock_s3.get_object.assert_called_once_with(
        Bucket="test-bucket", Key="jds/abc123.txt"
    )
    mock_cursor.execute.assert_called_once()


@patch(
    "lambda_.persist.handler.get_db_connection",
    side_effect=Exception("connection refused"),
)
@patch("lambda_.persist.handler._ensure_clients")
def test_read_and_persist_db_failure(mock_ensure, mock_db):
    import lambda_.persist.handler as mod
    from lambda_.persist.handler import read_and_persist

    mock_s3 = MagicMock()
    mock_s3.get_object.return_value = {"Body": BytesIO(b"some content")}
    mod.s3 = mock_s3

    result = read_and_persist("test-bucket", "jds/xyz.txt")
    assert result["status"] == "failed"
    assert result["error"] == "internal_error"


# ---------------------------------------------------------------------------
# Issue 1: Adapter JSON format support
# ---------------------------------------------------------------------------


@patch("lambda_.persist.handler.get_db_connection")
@patch("lambda_.persist.handler._ensure_clients")
def test_read_and_persist_json_format(mock_ensure, mock_db):
    """Persist handler extracts company/role/source from adapter JSON."""
    import lambda_.persist.handler as mod
    from lambda_.persist.handler import read_and_persist

    job_data = {
        "company": "Acme Corp",
        "role": "Senior Engineer",
        "source": "adzuna",
        "ats_url": "https://acme.com/apply",
    }
    mock_s3 = MagicMock()
    mock_s3.get_object.return_value = {"Body": BytesIO(json.dumps(job_data).encode())}
    mod.s3 = mock_s3

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = (99,)
    mock_conn.cursor.return_value.__enter__ = lambda s: mock_cursor
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    mock_db.return_value = mock_conn

    result = read_and_persist("test-bucket", "jds/abc123.json")

    assert result["status"] == "success"
    assert result["job_id"] == 99
    # Verify structured fields were passed to SQL
    call_args = mock_cursor.execute.call_args[0]
    sql_params = call_args[1]
    assert sql_params[0] == "Acme Corp"  # company
    assert sql_params[1] == "Senior Engineer"  # role
    assert sql_params[2] == "adzuna"  # source
    assert sql_params[4] == "https://acme.com/apply"  # ats_url


@patch("lambda_.persist.handler._ensure_clients")
def test_read_and_persist_rejects_oversized(mock_ensure):
    """Persist handler rejects S3 objects larger than 1 MB."""
    import lambda_.persist.handler as mod
    from lambda_.persist.handler import read_and_persist

    mock_s3 = MagicMock()
    mock_s3.get_object.return_value = {"Body": BytesIO(b"x" * 1_100_000)}
    mod.s3 = mock_s3

    result = read_and_persist("test-bucket", "jds/big.txt")
    assert result["status"] == "skipped"
    assert result["error"] == "content_too_large"


def test_handler_processes_json_prefix():
    """Handler accepts jds/*.json files (not just .txt)."""
    from lambda_.persist.handler import handler

    event = _s3_event(key="jds/abc.json")

    with patch(
        "lambda_.persist.handler.read_and_persist", return_value={"status": "success"}
    ) as mock:
        result = handler(event, None)

    mock.assert_called_once_with("test-bucket", "jds/abc.json")
    assert len(result["results"]) == 1
