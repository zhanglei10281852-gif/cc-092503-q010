from __future__ import annotations

from datetime import UTC, datetime

from app.core.clock import FrozenClock
from app.services.jobs import JobService


def test_background_job_claim_complete_and_deduplicate(client):
    from app.database import transaction

    clock = FrozenClock(datetime(2026, 9, 25, 8, 30, tzinfo=UTC))
    with transaction(immediate=True) as connection:
        service = JobService(connection, clock)
        first = service.enqueue("inventory-summary", "inventory:2026-09-25", {"date": "2026-09-25"})
        second = service.enqueue("inventory-summary", "inventory:2026-09-25", {"date": "2026-09-25"})
        assert first["id"] == second["id"]
        claimed = service.claim("worker-1")
        assert claimed and claimed["status"] == "running"
        completed = service.complete(claimed["id"], "worker-1", {"count": 3})
        assert completed["status"] == "completed"
