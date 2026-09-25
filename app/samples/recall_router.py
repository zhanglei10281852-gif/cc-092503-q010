from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status

from app.api.dependencies import current_principal
from app.core.security import Principal
from app.database import get_connection, transaction
from app.samples.recall import ContaminationService
from app.samples.recall_schemas import (
    ContaminationEventCreate,
    InvestigationConclusion,
    ReleaseDecision,
    ReleaseRequestCreate,
)

router = APIRouter(prefix="/api/contamination", tags=["污染影响追踪与召回"])


@router.post("/events", status_code=status.HTTP_201_CREATED)
def create_contamination_event(payload: ContaminationEventCreate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return ContaminationService(connection).create_event(principal, payload.model_dump())


@router.get("/events")
def list_contamination_events(
    state: str | None = Query(default=None),
    principal: Principal = Depends(current_principal),
):
    return ContaminationService(get_connection()).list_events(principal, state)


@router.get("/events/{event_id}")
def contamination_event_detail(event_id: int, principal: Principal = Depends(current_principal)):
    return ContaminationService(get_connection()).detail(principal, event_id)


@router.post("/events/{event_id}/recalculate")
def recalculate_containment(event_id: int, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return ContaminationService(connection).recalculate(principal, event_id)


@router.post("/events/{event_id}/investigation")
def record_investigation(event_id: int, payload: InvestigationConclusion, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return ContaminationService(connection).record_investigation(principal, event_id, payload.model_dump())


@router.post("/events/{event_id}/release-requests", status_code=status.HTTP_201_CREATED)
def request_release(event_id: int, payload: ReleaseRequestCreate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return ContaminationService(connection).request_release(principal, event_id, payload.model_dump())


@router.post("/release-requests/{request_id}/decisions")
def decide_release(request_id: int, payload: ReleaseDecision, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return ContaminationService(connection).decide_release(principal, request_id, payload.model_dump())
