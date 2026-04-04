"""Tests for the validation pipeline in local/pipeline/validator.py."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pydantic import ValidationError


# ---------------------------------------------------------------------------
# StatusPayload validation
# ---------------------------------------------------------------------------


class TestValidateStatus:
    @pytest.mark.asyncio
    async def test_valid_status_passes(self, mock_db_conn, sample_status_payload):
        mock_db_conn.fetchval = AsyncMock(return_value=True)

        with patch("local.pipeline.validator.acquire") as mock_acq:
            mock_acq.return_value = _async_cm(mock_db_conn)
            with patch("local.pipeline.validator._log_metric", new_callable=AsyncMock):
                from local.pipeline.validator import validate_status

                result = await validate_status(sample_status_payload)

        assert result.job_id == 1
        assert result.stage == "applied"

    @pytest.mark.asyncio
    async def test_invalid_stage_rejected(self):
        from local.pipeline.validator import validate_status

        with pytest.raises(ValidationError):
            await validate_status({"job_id": 1, "stage": "hired"})

    @pytest.mark.asyncio
    async def test_nonexistent_job_id_rejected(self, mock_db_conn):
        mock_db_conn.fetchval = AsyncMock(return_value=False)

        with patch("local.pipeline.validator.acquire") as mock_acq:
            mock_acq.return_value = _async_cm(mock_db_conn)
            from local.pipeline.validator import validate_status

            with pytest.raises(ValueError, match="not found"):
                await validate_status({"job_id": 9999, "stage": "applied"})


# ---------------------------------------------------------------------------
# RecommendationPayload validation
# ---------------------------------------------------------------------------


class TestValidateRecommendation:
    @pytest.mark.asyncio
    async def test_valid_recommendation_passes(
        self, mock_db_conn, sample_recommendation_payload
    ):
        with patch("local.pipeline.validator._get_redactor") as mock_red:
            mock_red.return_value = MagicMock(
                contains_pii=MagicMock(return_value=False)
            )
            with patch(
                "local.pipeline.validator.is_company_allowed",
                new_callable=AsyncMock,
                return_value=True,
            ):
                with patch(
                    "local.pipeline.validator._log_metric", new_callable=AsyncMock
                ):
                    from local.pipeline.validator import validate_recommendation

                    result = await validate_recommendation(
                        sample_recommendation_payload
                    )

        assert result.company == "Anthropic"
        assert result.role == "Software Engineer"

    @pytest.mark.asyncio
    async def test_pii_in_company_rejected(self, sample_recommendation_payload):
        sample_recommendation_payload["company"] = "John Doe"

        with patch("local.pipeline.validator._get_redactor") as mock_red:
            mock_red.return_value = MagicMock(contains_pii=MagicMock(return_value=True))
            with patch("local.pipeline.validator._log_metric", new_callable=AsyncMock):
                from local.pipeline.validator import validate_recommendation

                with pytest.raises(ValueError, match="PII detected"):
                    await validate_recommendation(sample_recommendation_payload)

    @pytest.mark.asyncio
    async def test_special_chars_in_company_rejected(self):
        from local.pipeline.validator import validate_recommendation

        with pytest.raises(ValidationError, match="Invalid characters"):
            await validate_recommendation(
                {
                    "company": "Evil<script>",
                    "role": "Engineer",
                }
            )

    @pytest.mark.asyncio
    async def test_company_max_length_rejected(self):
        from local.pipeline.validator import validate_recommendation

        with pytest.raises(ValidationError):
            await validate_recommendation(
                {
                    "company": "A" * 101,
                    "role": "Engineer",
                }
            )


# ---------------------------------------------------------------------------
# FollowupPayload validation
# ---------------------------------------------------------------------------


class TestValidateFollowup:
    @pytest.mark.asyncio
    async def test_valid_followup_passes(self, mock_db_conn, sample_followup_payload):
        mock_db_conn.fetchval = AsyncMock(return_value=True)

        with patch("local.pipeline.validator.acquire") as mock_acq:
            mock_acq.return_value = _async_cm(mock_db_conn)
            with patch("local.pipeline.validator._log_metric", new_callable=AsyncMock):
                from local.pipeline.validator import validate_followup

                result = await validate_followup(sample_followup_payload)

        assert result.job_id == 1
        assert result.urgency == "high"
        assert result.action == "send_followup"

    @pytest.mark.asyncio
    async def test_nonexistent_job_id_rejected(self, mock_db_conn):
        mock_db_conn.fetchval = AsyncMock(return_value=False)

        with patch("local.pipeline.validator.acquire") as mock_acq:
            mock_acq.return_value = _async_cm(mock_db_conn)
            from local.pipeline.validator import validate_followup

            with pytest.raises(ValueError, match="not found"):
                await validate_followup(
                    {
                        "job_id": 9999,
                        "urgency": "high",
                        "action": "send_followup",
                    }
                )


# ---------------------------------------------------------------------------
# Metric logging
# ---------------------------------------------------------------------------


class TestPipelineMetrics:
    @pytest.mark.asyncio
    async def test_log_metric_called_on_success(self, mock_db_conn):
        mock_db_conn.fetchval = AsyncMock(return_value=True)

        with patch("local.pipeline.validator.acquire") as mock_acq:
            mock_acq.return_value = _async_cm(mock_db_conn)
            with patch(
                "local.pipeline.validator._log_metric", new_callable=AsyncMock
            ) as mock_log:
                from local.pipeline.validator import validate_status

                await validate_status({"job_id": 1, "stage": "applied"})

                mock_log.assert_awaited_once_with("status", "validated")


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


class _async_cm:
    """Simple async context manager wrapper for mocks."""

    def __init__(self, val):
        self._val = val

    async def __aenter__(self):
        return self._val

    async def __aexit__(self, *args):
        pass
