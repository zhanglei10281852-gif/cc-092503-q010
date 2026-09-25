from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from app.core.clock import Clock, SystemClock, to_storage
from app.repositories.audit import AuditRepository


@dataclass(slots=True)
class AuditContext:
    actor_user_id: int | None
    actor_name: str
    correlation_id: str | None = None


class AuditService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None) -> None:
        self.repository = AuditRepository(connection)
        self.clock = clock or SystemClock()

    def record(
        self,
        context: AuditContext | object,
        action: str | None = None,
        resource_type: str | None = None,
        resource_id: str | int | None = None,
        *,
        outcome: str = "success",
        before: dict | None = None,
        after: dict | None = None,
        metadata: dict | None = None,
    ) -> int:
        if action is None or resource_type is None:
            raise ValueError("审计动作和资源类型不能为空")
        if not isinstance(context, AuditContext):
            context = AuditContext(
                actor_user_id=getattr(context, "user_id", None),
                actor_name=str(getattr(context, "display_name", "系统")),
                correlation_id=getattr(context, "correlation_id", None),
            )
        return self.repository.append(
            actor_user_id=context.actor_user_id,
            actor_name=context.actor_name,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome=outcome,
            before=before,
            after=after,
            metadata=metadata,
            correlation_id=context.correlation_id,
            created_at=to_storage(self.clock.now()),
        )
