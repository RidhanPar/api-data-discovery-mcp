"""Audit log: append-only record of who did what to which resource, with what outcome."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import AUDIT_DECISIONS, AuditEvent

Decision = str  # one of AUDIT_DECISIONS (validated below)


class AuditEventIn(BaseModel):
    actor: str = Field(min_length=1, max_length=200)
    action: str = Field(pattern=r"^[a-z][a-z0-9_.:-]{1,79}$")
    resource: str | None = Field(None, max_length=300)
    decision: str
    reason: str | None = Field(None, max_length=2000)
    request_id: str | None = Field(None, max_length=80)
    details: dict[str, Any] = Field(default_factory=dict)

    def model_post_init(self, _: Any) -> None:
        if self.decision not in AUDIT_DECISIONS:
            raise ValueError(f"decision must be one of {AUDIT_DECISIONS}")


class AuditEventOut(AuditEventIn):
    id: int
    occurred_at: datetime
    source: str


def record(session: Session, source: str, event: AuditEventIn) -> AuditEventOut:
    row = AuditEvent(source=source, **event.model_dump())
    session.add(row)
    session.flush()
    session.refresh(row)
    return _out(row)


def write(
    session: Session,
    *,
    source: str,
    actor: str,
    action: str,
    decision: str,
    resource: str | None = None,
    reason: str | None = None,
    request_id: str | None = None,
    **details: Any,
) -> None:
    record(
        session,
        source,
        AuditEventIn(
            actor=actor,
            action=action,
            resource=resource,
            decision=decision,
            reason=reason,
            request_id=request_id,
            details=details,
        ),
    )


def _out(r: AuditEvent) -> AuditEventOut:
    return AuditEventOut(
        id=r.id,
        occurred_at=r.occurred_at,
        source=r.source,
        actor=r.actor,
        action=r.action,
        resource=r.resource,
        decision=r.decision,
        reason=r.reason,
        request_id=r.request_id,
        details=r.details,
    )


def query(
    session: Session,
    *,
    actor: str | None = None,
    action: str | None = None,
    request_id: str | None = None,
    limit: int = 100,
) -> list[AuditEventOut]:
    stmt = select(AuditEvent).order_by(AuditEvent.id.desc()).limit(limit)
    if actor:
        stmt = stmt.where(AuditEvent.actor == actor)
    if action:
        stmt = stmt.where(AuditEvent.action == action)
    if request_id:
        stmt = stmt.where(AuditEvent.request_id == request_id)
    return [_out(r) for r in session.scalars(stmt)]
