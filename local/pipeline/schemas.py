"""Pydantic schemas for local → cloud validation pipeline.

These models enforce the IDs+enums-only contract: only structured,
non-PII data crosses the boundary to cloud endpoints.
"""

import re
from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

COMPANY_REGEX = re.compile(r"^[a-zA-Z0-9\s.,&'()\-/!:;#+@\u2013\u2014]+$")


class StatusPayload(BaseModel):
    job_id: int
    stage: Literal[
        "to_apply",
        "waiting_for_referral",
        "applied",
        "assessment",
        "assignment",
        "interview",
        "offer",
        "rejected",
    ]
    deadline: Optional[date] = None


class RecommendationPayload(BaseModel):
    company: str = Field(max_length=100)
    role: str = Field(max_length=200)

    @field_validator("company", "role")
    @classmethod
    def validate_format(cls, v):
        if not COMPANY_REGEX.match(v):
            raise ValueError(f"Invalid characters in field: {v}")
        return v.strip()


class FollowupPayload(BaseModel):
    job_id: int
    urgency: Literal["high", "medium", "low"]
    action: Literal["send_followup", "check_status", "withdraw"]
