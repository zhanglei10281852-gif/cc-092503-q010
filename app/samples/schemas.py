from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class LocationCreate(BaseModel):
    code: str = Field(min_length=2, max_length=64)
    building: str = Field(min_length=1, max_length=100)
    room: str = Field(min_length=1, max_length=100)
    cabinet: str = Field(min_length=1, max_length=100)
    shelf: str = Field(min_length=1, max_length=100)
    sensitivity: Literal["normal", "restricted", "critical"] = "normal"
    capacity_units: int = Field(gt=0, le=1_000_000)


class BatchCreate(BaseModel):
    batch_code: str = Field(min_length=3, max_length=64)
    project_code: str = Field(min_length=2, max_length=64)
    expected_count: int = Field(gt=0, le=100_000)


class SampleCreate(BaseModel):
    sample_code: str = Field(min_length=3, max_length=100)
    batch_id: int = Field(gt=0)
    collection_event_id: int | None = Field(default=None, gt=0)
    sample_type: str = Field(min_length=1, max_length=100)
    quantity: float = Field(gt=0)
    unit: str = Field(min_length=1, max_length=20)
    location_id: int | None = Field(default=None, gt=0)


class AliquotChild(BaseModel):
    sample_code: str = Field(min_length=3, max_length=100)
    quantity: float = Field(gt=0)
    location_id: int | None = Field(default=None, gt=0)


class AliquotRequest(BaseModel):
    operation_code: str | None = Field(default=None, max_length=64)
    requested_quantity: float = Field(gt=0)
    loss_quantity: float = Field(default=0, ge=0)
    children: list[AliquotChild] = Field(min_length=1, max_length=100)
    note: str = Field(default="", max_length=500)


class ConsumptionCreate(BaseModel):
    experiment_code: str = Field(min_length=2, max_length=100)
    quantity: float = Field(gt=0)
    idempotency_key: str = Field(min_length=4, max_length=100)
    note: str = Field(default="", max_length=500)


class LoanCreate(BaseModel):
    loan_code: str | None = Field(default=None, max_length=64)
    sample_id: int = Field(gt=0)
    borrower_user_id: int = Field(gt=0)
    quantity: float = Field(gt=0)
    due_at: str = Field(min_length=10, max_length=40)


class LoanReturn(BaseModel):
    quantity: float = Field(gt=0)
    note: str = Field(default="", max_length=500)


class ApprovalCreate(BaseModel):
    request_code: str | None = Field(default=None, max_length=64)
    action_type: Literal["loan", "destruction", "location_reveal", "inventory_adjustment"]
    resource_type: str = Field(min_length=2, max_length=50)
    resource_id: int = Field(gt=0)
    payload: dict[str, Any] = Field(default_factory=dict)
    expires_at: str | None = None


class ApprovalDecision(BaseModel):
    decision: Literal["approve", "reject"]
    comment: str = Field(default="", max_length=500)


class AnomalyCreate(BaseModel):
    case_code: str | None = Field(default=None, max_length=64)
    sample_id: int | None = Field(default=None, gt=0)
    batch_id: int | None = Field(default=None, gt=0)
    anomaly_type: str = Field(min_length=2, max_length=100)
    severity: Literal["low", "medium", "high", "critical"]
    description: str = Field(min_length=4, max_length=2000)

    @model_validator(mode="after")
    def ensure_target(self):
        if not self.sample_id and not self.batch_id:
            raise ValueError("sample_id 与 batch_id 至少填写一个")
        return self
