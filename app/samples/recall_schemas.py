from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ContaminationOpen(BaseModel):
    source_sample_id: int = Field(gt=0)
    source_event_id: int = Field(gt=0)
    contaminant_label: str = Field(min_length=2, max_length=200)
    description: str = Field(default="", max_length=2000)
    case_code: str | None = Field(default=None, min_length=3, max_length=64)
    idempotency_key: str | None = Field(default=None, min_length=4, max_length=100)


class ReleaseRequestCreate(BaseModel):
    conclusion: str = Field(min_length=10, max_length=2000)


class ReleaseDecision(BaseModel):
    decision: Literal["approve", "reject"]
    comment: str = Field(default="", max_length=500)
