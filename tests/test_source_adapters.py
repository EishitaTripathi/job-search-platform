"""Tests for Lambda fetch source adapters and registry."""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# The adapter_registry uses relative imports from lambda/fetch/.
# We import adapters directly from their file paths.
ROOT = Path(__file__).parent.parent
FETCH_DIR = ROOT / "lambda" / "fetch"

# Ensure lambda/fetch and lambda/fetch/adapters are importable
sys.path.insert(0, str(FETCH_DIR))


# ---------------------------------------------------------------------------
# NormalizedJob dataclass
# ---------------------------------------------------------------------------


class TestNormalizedJob:
    def test_fields(self):
        from adapters.base import NormalizedJob

        job = NormalizedJob(
            company="Anthropic",
            role="Software Engineer",
            location="San Francisco, CA",
            ats_url="https://boards.greenhouse.io/anthropic/jobs/123",
            date_posted="2026-03-20",
            source="greenhouse",
            source_id="123",
            raw_json={"id": 123},
        )
        assert job.company == "Anthropic"
        assert job.source == "greenhouse"
        assert isinstance(job.raw_json, dict)

    def test_optional_date_posted(self):
        from adapters.base import NormalizedJob

        job = NormalizedJob(
            company="Test",
            role="Test",
            location="Remote",
            ats_url="",
            date_posted=None,
            source="test",
            source_id="1",
            raw_json={},
        )
        assert job.date_posted is None


# ---------------------------------------------------------------------------
# SSRF validation in base adapter
# ---------------------------------------------------------------------------


class TestBaseAdapterSSRF:
    def test_rejects_file_scheme(self):
        from adapters.base import SourceAdapter

        class DummyAdapter(SourceAdapter):
            def fetch(self, params):
                return []

        adapter = DummyAdapter()
        assert adapter._validate_url("file:///etc/passwd") is False

    def test_rejects_ftp_scheme(self):
        from adapters.base import SourceAdapter

        class DummyAdapter(SourceAdapter):
            def fetch(self, params):
                return []

        adapter = DummyAdapter()
        assert adapter._validate_url("ftp://evil.com/file") is False

    def test_rejects_private_ip(self):
        from adapters.base import SourceAdapter

        class DummyAdapter(SourceAdapter):
            def fetch(self, params):
                return []

        adapter = DummyAdapter()
        assert adapter._validate_url("http://10.0.0.1/admin") is False

    def test_rejects_loopback(self):
        from adapters.base import SourceAdapter

        class DummyAdapter(SourceAdapter):
            def fetch(self, params):
                return []

        adapter = DummyAdapter()
        assert adapter._validate_url("http://127.0.0.1/admin") is False

    def test_rejects_aws_metadata(self):
        from adapters.base import SourceAdapter

        class DummyAdapter(SourceAdapter):
            def fetch(self, params):
                return []

        adapter = DummyAdapter()
        assert adapter._validate_url("http://169.254.169.254/latest/") is False

    def test_allows_public_hostname(self):
        from adapters.base import SourceAdapter

        class DummyAdapter(SourceAdapter):
            def fetch(self, params):
                return []

        adapter = DummyAdapter()
        assert adapter._validate_url("https://boards.greenhouse.io/company") is True

    def test_allows_https_public_url(self):
        from adapters.base import SourceAdapter

        class DummyAdapter(SourceAdapter):
            def fetch(self, params):
                return []

        adapter = DummyAdapter()
        assert adapter._validate_url("https://api.adzuna.com/v1/jobs") is True


# ---------------------------------------------------------------------------
# Adapter registry
# ---------------------------------------------------------------------------


class TestAdapterRegistry:
    def test_returns_adapter_for_each_source(self):
        from adapter_registry import ADAPTERS, get_adapter

        for source_name in ADAPTERS:
            adapter = get_adapter(source_name)
            assert adapter.source_name == source_name

    def test_raises_for_unknown_source(self):
        from adapter_registry import get_adapter

        with pytest.raises(ValueError, match="Unknown source"):
            get_adapter("nonexistent_source")

    def test_all_known_sources(self):
        from adapter_registry import ADAPTERS

        expected = {
            "simplify",
            "the_muse",
            "greenhouse",
            "lever",
            "ashby",
            "hn_hiring",
        }
        assert set(ADAPTERS.keys()) == expected


# ---------------------------------------------------------------------------
# SimplifyAdapter parsing
# ---------------------------------------------------------------------------


class TestSimplifyAdapter:
    def test_parses_sample_json(self):
        from adapters.simplify import SimplifyAdapter

        sample_response = [
            {
                "id": "abc123",
                "company_name": "Anthropic",
                "title": "Software Engineer",
                "locations": ["San Francisco, CA"],
                "url": "https://boards.greenhouse.io/anthropic/jobs/123",
                "date_posted": 1742428800,  # Unix timestamp
                "active": True,
                "sponsorship": "Offers Sponsorship",
            },
            {
                "id": "def456",
                "company_name": "OpenAI",
                "title": "ML Engineer",
                "locations": ["Remote"],
                "url": "https://openai.com/careers/456",
                "date_posted": 1742342400,
                "active": True,
                "sponsorship": "Other",
            },
            {
                # Missing required fields - should be skipped
                "id": "skip",
                "company_name": "",
                "title": "",
                "active": True,
                "sponsorship": "Other",
            },
            {
                # Inactive - should be skipped
                "id": "inactive",
                "company_name": "Closed Co",
                "title": "Engineer",
                "active": False,
                "sponsorship": "Offers Sponsorship",
            },
            {
                # No sponsorship - should be skipped
                "id": "no_visa",
                "company_name": "US Only Corp",
                "title": "Engineer",
                "active": True,
                "sponsorship": "U.S. Citizenship is Required",
            },
        ]

        adapter = SimplifyAdapter()

        with patch("adapters.simplify.urllib.request.urlopen") as mock_urlopen:
            import json

            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(sample_response).encode()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp

            results = adapter.fetch({})

        assert len(results) == 2
        assert results[0].company == "Anthropic"
        assert results[0].source == "simplify"
        assert results[1].company == "OpenAI"


# ---------------------------------------------------------------------------
# AdzunaAdapter parsing
# ---------------------------------------------------------------------------


class TestAdzunaAdapter:
    def test_parses_sample_response(self, monkeypatch):
        monkeypatch.setenv("ADZUNA_APP_ID", "test-id")
        monkeypatch.setenv("ADZUNA_APP_KEY", "test-key")

        from adapters.adzuna import AdzunaAdapter

        sample_response = {
            "results": [
                {
                    "id": "12345",
                    "title": "Senior Backend Engineer",
                    "company": {"display_name": "Stripe"},
                    "location": {"display_name": "San Francisco"},
                    "redirect_url": "https://adzuna.com/land/job/12345",
                    "created": "2026-03-18T10:00:00Z",
                },
            ],
        }

        adapter = AdzunaAdapter()

        with patch("adapters.adzuna.urllib.request.urlopen") as mock_urlopen:
            import json

            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(sample_response).encode()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp

            results = adapter.fetch({"query": "backend engineer"})

        assert len(results) == 1
        assert results[0].company == "Stripe"
        assert results[0].source == "adzuna"
        assert results[0].date_posted == "2026-03-18"

    def test_returns_empty_without_credentials(self, monkeypatch):
        monkeypatch.delenv("ADZUNA_APP_ID", raising=False)
        monkeypatch.delenv("ADZUNA_APP_KEY", raising=False)

        from adapters.adzuna import AdzunaAdapter

        adapter = AdzunaAdapter()
        results = adapter.fetch({})
        assert results == []
