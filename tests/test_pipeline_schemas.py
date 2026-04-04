"""Tests for Pydantic validation schemas in local/pipeline/schemas.py."""

import pytest
from datetime import date
from pydantic import ValidationError

from local.pipeline.schemas import (
    StatusPayload,
    RecommendationPayload,
    FollowupPayload,
)


# ---------------------------------------------------------------------------
# StatusPayload
# ---------------------------------------------------------------------------


class TestStatusPayload:
    def test_valid_minimal(self):
        p = StatusPayload(job_id=1, stage="applied")
        assert p.job_id == 1
        assert p.stage == "applied"
        assert p.deadline is None

    def test_valid_with_deadline(self):
        p = StatusPayload(job_id=42, stage="interview", deadline=date(2026, 4, 15))
        assert p.deadline == date(2026, 4, 15)

    def test_all_valid_stages(self):
        stages = [
            "to_apply",
            "waiting_for_referral",
            "applied",
            "assessment",
            "assignment",
            "interview",
            "offer",
            "rejected",
        ]
        for stage in stages:
            p = StatusPayload(job_id=1, stage=stage)
            assert p.stage == stage

    def test_invalid_stage_rejected(self):
        with pytest.raises(ValidationError):
            StatusPayload(job_id=1, stage="hired")

    def test_missing_job_id_rejected(self):
        with pytest.raises(ValidationError):
            StatusPayload(stage="applied")

    def test_string_job_id_coerced(self):
        """Pydantic coerces numeric strings to int."""
        p = StatusPayload(job_id="5", stage="applied")
        assert p.job_id == 5

    def test_non_numeric_job_id_rejected(self):
        with pytest.raises(ValidationError):
            StatusPayload(job_id="abc", stage="applied")

    def test_deadline_string_coerced(self):
        """Pydantic accepts ISO date strings for date fields."""
        p = StatusPayload(job_id=1, stage="applied", deadline="2026-04-15")
        assert p.deadline == date(2026, 4, 15)

    def test_invalid_deadline_rejected(self):
        with pytest.raises(ValidationError):
            StatusPayload(job_id=1, stage="applied", deadline="not-a-date")


# ---------------------------------------------------------------------------
# RecommendationPayload
# ---------------------------------------------------------------------------


class TestRecommendationPayload:
    def test_valid(self):
        p = RecommendationPayload(company="Anthropic", role="Software Engineer")
        assert p.company == "Anthropic"
        assert p.role == "Software Engineer"

    def test_company_with_special_chars_allowed(self):
        """Ampersand, period, comma, parentheses, apostrophe, hyphen are valid."""
        p = RecommendationPayload(company="AT&T (Mobility)", role="SWE")
        assert p.company == "AT&T (Mobility)"

    def test_company_with_invalid_chars_rejected(self):
        with pytest.raises(ValidationError, match="Invalid characters"):
            RecommendationPayload(company="Evil<script>Co", role="SWE")

    def test_role_with_invalid_chars_rejected(self):
        with pytest.raises(ValidationError, match="Invalid characters"):
            RecommendationPayload(company="Acme", role="Engineer; DROP TABLE")

    def test_company_max_length(self):
        with pytest.raises(ValidationError):
            RecommendationPayload(company="A" * 101, role="SWE")

    def test_role_max_length(self):
        with pytest.raises(ValidationError):
            RecommendationPayload(company="Acme", role="A" * 201)

    def test_company_at_max_length(self):
        p = RecommendationPayload(company="A" * 100, role="SWE")
        assert len(p.company) == 100

    def test_empty_company_rejected(self):
        with pytest.raises(ValidationError):
            RecommendationPayload(company="", role="SWE")

    def test_empty_role_rejected(self):
        with pytest.raises(ValidationError):
            RecommendationPayload(company="Acme", role="")

    def test_whitespace_stripped(self):
        p = RecommendationPayload(company="  Anthropic  ", role="  SWE  ")
        assert p.company == "Anthropic"
        assert p.role == "SWE"

    def test_missing_company_rejected(self):
        with pytest.raises(ValidationError):
            RecommendationPayload(role="SWE")

    def test_missing_role_rejected(self):
        with pytest.raises(ValidationError):
            RecommendationPayload(company="Acme")


# ---------------------------------------------------------------------------
# FollowupPayload
# ---------------------------------------------------------------------------


class TestFollowupPayload:
    def test_valid(self):
        p = FollowupPayload(job_id=1, urgency="high", action="send_followup")
        assert p.job_id == 1
        assert p.urgency == "high"
        assert p.action == "send_followup"

    def test_all_urgency_levels(self):
        for level in ["high", "medium", "low"]:
            p = FollowupPayload(job_id=1, urgency=level, action="check_status")
            assert p.urgency == level

    def test_all_actions(self):
        for action in ["send_followup", "check_status", "withdraw"]:
            p = FollowupPayload(job_id=1, urgency="low", action=action)
            assert p.action == action

    def test_invalid_urgency_rejected(self):
        with pytest.raises(ValidationError):
            FollowupPayload(job_id=1, urgency="critical", action="send_followup")

    def test_invalid_action_rejected(self):
        with pytest.raises(ValidationError):
            FollowupPayload(job_id=1, urgency="high", action="delete_everything")

    def test_missing_job_id_rejected(self):
        with pytest.raises(ValidationError):
            FollowupPayload(urgency="high", action="send_followup")

    def test_none_urgency_rejected(self):
        with pytest.raises(ValidationError):
            FollowupPayload(job_id=1, urgency=None, action="send_followup")
