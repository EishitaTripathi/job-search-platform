"""Tests for SSRF protection — URL validation in fetch handlers."""

import pytest
from unittest.mock import patch


@pytest.fixture(autouse=True)
def env_vars(monkeypatch):
    monkeypatch.setenv("S3_BUCKET", "test-bucket")


def test_rejects_file_scheme():
    from lambda_.fetch.handler import _validate_url

    with pytest.raises(ValueError, match="Disallowed URL scheme"):
        _validate_url("file:///etc/passwd")


def test_rejects_ftp_scheme():
    from lambda_.fetch.handler import _validate_url

    with pytest.raises(ValueError, match="Disallowed URL scheme"):
        _validate_url("ftp://example.com/file")


def test_rejects_no_hostname():
    from lambda_.fetch.handler import _validate_url

    with pytest.raises(ValueError, match="no hostname"):
        _validate_url("http://")


@patch(
    "lambda_.fetch.handler.socket.getaddrinfo",
    return_value=[
        (None, None, None, None, ("127.0.0.1", 0)),
    ],
)
def test_rejects_loopback(mock_dns):
    from lambda_.fetch.handler import _validate_url

    with pytest.raises(ValueError, match="disallowed IP"):
        _validate_url("http://localhost/admin")


@patch(
    "lambda_.fetch.handler.socket.getaddrinfo",
    return_value=[
        (None, None, None, None, ("169.254.169.254", 0)),
    ],
)
def test_rejects_aws_metadata(mock_dns):
    from lambda_.fetch.handler import _validate_url

    with pytest.raises(ValueError, match="disallowed IP"):
        _validate_url("http://169.254.169.254/latest/meta-data/")


@patch(
    "lambda_.fetch.handler.socket.getaddrinfo",
    return_value=[
        (None, None, None, None, ("10.0.0.1", 0)),
    ],
)
def test_rejects_private_ip(mock_dns):
    from lambda_.fetch.handler import _validate_url

    with pytest.raises(ValueError, match="disallowed IP"):
        _validate_url("http://internal-service.local/api")


@patch(
    "lambda_.fetch.handler.socket.getaddrinfo",
    return_value=[
        (None, None, None, None, ("54.200.100.50", 0)),
    ],
)
def test_allows_public_url(mock_dns):
    from lambda_.fetch.handler import _validate_url

    # Should not raise
    _validate_url("https://boards.greenhouse.io/company/jobs/123")
