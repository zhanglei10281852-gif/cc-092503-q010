from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status

from app.api.dependencies import current_principal
from app.database import get_connection, transaction
from app.core.security import Principal
from app.samples.schemas import (
    AliquotRequest,
    AnomalyCreate,
    ApprovalCreate,
    ApprovalDecision,
    BatchCreate,
    ConsumptionCreate,
    LoanCreate,
    LoanReturn,
    LocationCreate,
    SampleCreate,
)
from app.samples.service import AnomalyService, ApprovalService, LoanService, LocationService, SampleLifecycleService

router = APIRouter(prefix="/api/samples", tags=["科研样品"])


@router.post("/locations", status_code=status.HTTP_201_CREATED)
def create_location(payload: LocationCreate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return LocationService(connection).create(principal, payload.model_dump())


@router.get("/locations")
def list_locations(principal: Principal = Depends(current_principal)):
    return LocationService(get_connection()).list(principal)


@router.post("/batches", status_code=status.HTTP_201_CREATED)
def create_batch(payload: BatchCreate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return SampleLifecycleService(connection).create_batch(principal, payload.model_dump())


@router.post("", status_code=status.HTTP_201_CREATED)
def create_sample(payload: SampleCreate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return SampleLifecycleService(connection).register_sample(principal, payload.model_dump())


@router.get("")
def list_samples(
    lifecycle_state: str | None = Query(default=None),
    batch_id: int | None = Query(default=None),
    principal: Principal = Depends(current_principal),
):
    return SampleLifecycleService(get_connection()).list_samples(principal, lifecycle_state, batch_id)


@router.get("/{sample_id}")
def get_sample(sample_id: int, principal: Principal = Depends(current_principal)):
    return SampleLifecycleService(get_connection()).detail(principal, sample_id)


@router.post("/{sample_id}/aliquots", status_code=status.HTTP_201_CREATED)
def aliquot(sample_id: int, payload: AliquotRequest, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return SampleLifecycleService(connection).aliquot(principal, sample_id, payload.model_dump())


@router.post("/{sample_id}/consumptions", status_code=status.HTTP_201_CREATED)
def consume(sample_id: int, payload: ConsumptionCreate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return SampleLifecycleService(connection).consume(principal, sample_id, payload.model_dump())


@router.post("/loans", status_code=status.HTTP_201_CREATED)
def create_loan(payload: LoanCreate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return LoanService(connection).create(principal, payload.model_dump())


@router.post("/loans/{loan_id}/returns")
def return_loan(loan_id: int, payload: LoanReturn, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return LoanService(connection).return_loan(principal, loan_id, payload.model_dump())


@router.post("/approvals", status_code=status.HTTP_201_CREATED)
def create_approval(payload: ApprovalCreate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return ApprovalService(connection).create(principal, payload.model_dump())


@router.post("/approvals/{request_id}/decisions")
def decide_approval(request_id: int, payload: ApprovalDecision, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return ApprovalService(connection).decide(principal, request_id, payload.model_dump())


@router.post("/anomalies", status_code=status.HTTP_201_CREATED)
def create_anomaly(payload: AnomalyCreate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return AnomalyService(connection).create(principal, payload.model_dump())


@router.get("/anomalies/list")
def list_anomalies(state: str | None = None, principal: Principal = Depends(current_principal)):
    return AnomalyService(get_connection()).list(principal, state)
