from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status

from app.api.dependencies import current_principal
from app.core.security import Principal
from app.database import get_connection, transaction
from app.samples.recall import ContaminationRecallService
from app.samples.recall_schemas import ContaminationOpen, ReleaseDecision, ReleaseRequestCreate

router = APIRouter(prefix="/api/recalls", tags=["污染影响追踪与召回"])


@router.post("/contamination-events", status_code=status.HTTP_201_CREATED)
def open_contamination(payload: ContaminationOpen, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return ContaminationRecallService(connection).open_case(principal, payload.model_dump())


@router.get("/contamination-events")
def list_contamination(state: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    return ContaminationRecallService(get_connection()).list_cases(principal, state)


@router.get("/contamination-events/{case_id}")
def get_contamination(case_id: int, principal: Principal = Depends(current_principal)):
    return ContaminationRecallService(get_connection()).get_case(principal, case_id)


@router.post("/contamination-events/{case_id}/recalculate")
def recalculate_contamination(case_id: int, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return ContaminationRecallService(connection).recalculate(principal, case_id)


@router.get("/contamination-events/{case_id}/report")
def contamination_report(
    case_id: int,
    run_no: int | None = Query(default=None, ge=1),
    principal: Principal = Depends(current_principal),
):
    return ContaminationRecallService(get_connection()).get_report(principal, case_id, run_no)


@router.post("/contamination-events/{case_id}/release-requests", status_code=status.HTTP_201_CREATED)
def request_release(case_id: int, payload: ReleaseRequestCreate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return ContaminationRecallService(connection).request_release(principal, case_id, payload.model_dump())


@router.post("/release-requests/{request_id}/decisions")
def decide_release(request_id: int, payload: ReleaseDecision, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return ContaminationRecallService(connection).decide_release(principal, request_id, payload.model_dump())
