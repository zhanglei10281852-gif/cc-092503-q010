from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ContaminationEventCreate(BaseModel):
    source_sample_id: int = Field(gt=0)
    title: str = Field(min_length=2, max_length=200)
    description: str = Field(default="", max_length=2000)
    severity: Literal["low", "medium", "high", "critical"] = "high"
    event_code: str | None = Field(default=None, max_length=64)


class InvestigationConclusion(BaseModel):
    conclusion: str = Field(min_length=4, max_length=4000)


class ReleaseRequestCreate(BaseModel):
    request_code: str | None = Field(default=None, max_length=64)
    note: str = Field(default="", max_length=500)
    expires_at: str | None = Field(default=None, max_length=40)


class ReleaseDecision(BaseModel):
    decision: Literal["approve", "reject"]
    comment: str = Field(default="", max_length=500)
