"""Tests for PII boundary enforcement — CLAUDE.md security rule."""

import pytest
from unittest.mock import patch, MagicMock


@pytest.fixture
def mock_analyzer():
    """Mock Presidio to avoid loading spaCy in tests."""
    with (
        patch("local.agents.shared.redactor.AnalyzerEngine") as mock_ae,
        patch("local.agents.shared.redactor.AnonymizerEngine") as mock_anon,
    ):
        yield mock_ae, mock_anon


def test_raises_on_pii(mock_analyzer):
    mock_ae_cls, _ = mock_analyzer
    mock_instance = MagicMock()
    # Simulate PII detection
    mock_instance.analyze.return_value = [MagicMock()]  # non-empty = PII found
    mock_ae_cls.return_value = mock_instance

    from local.agents.shared.redactor import enforce_pii_boundary

    # Reset singleton so our mock is used
    import local.agents.shared.redactor as mod

    mod._pii_redactor = None

    with pytest.raises(ValueError, match="PII detected"):
        enforce_pii_boundary({"name": "John Doe", "email": "john@example.com"})

    # Cleanup singleton
    mod._pii_redactor = None


def test_passes_on_clean_data(mock_analyzer):
    mock_ae_cls, _ = mock_analyzer
    mock_instance = MagicMock()
    # Simulate no PII detection
    mock_instance.analyze.return_value = []  # empty = no PII
    mock_ae_cls.return_value = mock_instance

    from local.agents.shared.redactor import enforce_pii_boundary
    import local.agents.shared.redactor as mod

    mod._pii_redactor = None

    # Should not raise
    enforce_pii_boundary({"title": "Software Engineer", "company": "Acme Corp"})

    # Cleanup singleton
    mod._pii_redactor = None


def test_skips_non_string_fields(mock_analyzer):
    mock_ae_cls, _ = mock_analyzer
    mock_instance = MagicMock()
    mock_instance.analyze.return_value = []
    mock_ae_cls.return_value = mock_instance

    from local.agents.shared.redactor import enforce_pii_boundary
    import local.agents.shared.redactor as mod

    mod._pii_redactor = None

    # Non-string fields should be skipped, not cause errors
    enforce_pii_boundary({"count": 42, "active": True, "label": "safe text"})

    # Cleanup singleton
    mod._pii_redactor = None
