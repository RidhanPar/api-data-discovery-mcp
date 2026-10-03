"""Access-request workflow (catalog service side).

Requests are always created as `pending_approval`. The only way to change status is
`decide()`, which a human approver calls; the database itself rejects a decision by
the requester (four-eyes) and an undecided status with a decider.

Phase 3 trusts the `requester` / `decided_by` values sent by the MCP server.
Phase 4 replaces that with identities taken from validated JWTs.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..access.policy import AccessRequirements, api_requirements, data_product_requirements, default_scope
from ..db.models import AccessRequest
from . import repository as repo

PURPOSE_PATTERN = r"^[a-z][a-z0-9_]{2,60}$"
IDENTITY_PATTERN = r"^[A-Za-z0-9._@:-]{1,200}$"


class AccessRuleViolation(ValueError):
    """The request breaks a rule (prohibited purpose, not reusable, unknown scope...)."""


class AccessRequestIn(BaseModel):
    requester: str = Field(pattern=IDENTITY_PATTERN)
    asset_type: Literal["api", "data_product"]
    asset_id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,119}$")
    version: int | None = Field(None, ge=1, le=99)
    purpose: str = Field(pattern=PURPOSE_PATTERN)
    justification: str = Field(min_length=20, max_length=2000)
    scope: str | None = Field(None, max_length=200)


class AccessRequestOut(BaseModel):
    id: uuid.UUID
    status: str
    requester: str
    asset_type: str
    asset_id: str
    major_version: int | None
    purpose: str
    justification: str
    requested_scope: str
    approval_route: str
    approvers: list[str]
    created_at: datetime
    decided_at: datetime | None
    decided_by: str | None
    decision_note: str | None
    created: bool = Field(True, description="False when an identical open request already existed")


class DecisionIn(BaseModel):
    decided_by: str = Field(pattern=IDENTITY_PATTERN)
    decision: Literal["approved", "rejected"]
    note: str = Field(min_length=3, max_length=2000)


def requirements(session: Session, asset_type: str, asset_id: str, version: int | None) -> AccessRequirements:
    if asset_type == "api":
        if version is None:
            raise AccessRuleViolation("version is required for APIs")
        return api_requirements(repo.api_details(session, asset_id, version).model_dump(mode="json"))
    return data_product_requirements(repo.data_product(session, asset_id).model_dump(mode="json"))


def _out(row: AccessRequest, created: bool = True) -> AccessRequestOut:
    return AccessRequestOut(
        id=row.id,
        status=row.status,
        requester=row.requester,
        asset_type=row.asset_type,
        asset_id=row.asset_id,
        major_version=row.major_version,
        purpose=row.purpose,
        justification=row.justification,
        requested_scope=row.requested_scope,
        approval_route=row.approval_route,
        approvers=row.approvers,
        created_at=row.created_at,
        decided_at=row.decided_at,
        decided_by=row.decided_by,
        decision_note=row.decision_note,
        created=created,
    )


def _find_open(session: Session, body: AccessRequestIn, major: int | None) -> AccessRequest | None:
    return session.scalar(
        select(AccessRequest).where(
            AccessRequest.requester == body.requester,
            AccessRequest.asset_type == body.asset_type,
            AccessRequest.asset_id == body.asset_id,
            AccessRequest.major_version.is_(None) if major is None else AccessRequest.major_version == major,
            AccessRequest.purpose == body.purpose,
            AccessRequest.status == "pending_approval",
        )
    )


def create(session: Session, body: AccessRequestIn) -> AccessRequestOut:
    req = requirements(session, body.asset_type, body.asset_id, body.version)
    if req.approval_route == "not_available_for_reuse":
        raise AccessRuleViolation(
            f"{body.asset_id} is not offered for reuse; request access to the system-of-record API"
        )
    if req.asset_type == "data_product":
        if body.purpose in req.prohibited_purposes:
            raise AccessRuleViolation(f"purpose '{body.purpose}' is prohibited for {body.asset_id}")
        if body.purpose not in (req.allowed_purposes or []):
            raise AccessRuleViolation(
                f"purpose '{body.purpose}' is not an allowed purpose for {body.asset_id}; "
                f"allowed: {', '.join(req.allowed_purposes or [])}"
            )
    scope = body.scope or default_scope(req)
    if scope is None:
        raise AccessRuleViolation(f"{body.asset_id} has no OAuth scope; contact team {req.owner_team}")
    if scope not in req.required_scopes:
        raise AccessRuleViolation(f"scope '{scope}' does not belong to {body.asset_id}: {req.required_scopes}")

    major = req.major_version
    existing = _find_open(session, body, major)
    if existing:
        return _out(existing, created=False)
    row = AccessRequest(
        id=uuid.uuid4(),
        requester=body.requester,
        asset_type=body.asset_type,
        asset_id=body.asset_id,
        major_version=major,
        purpose=body.purpose,
        justification=body.justification,
        requested_scope=scope,
        approval_route=req.approval_route,
        approvers=req.approvers,
        status="pending_approval",  # the ONLY status a request is ever created with
    )
    session.add(row)
    try:
        session.flush()
    except IntegrityError:  # a concurrent identical request won the race
        session.rollback()
        again = _find_open(session, body, major)
        if again is None:
            raise
        return _out(again, created=False)
    session.refresh(row)
    return _out(row)


def get(session: Session, request_id: uuid.UUID) -> AccessRequestOut:
    row = session.get(AccessRequest, request_id)
    if row is None:
        raise repo.NotFound(f"access request {request_id}")
    return _out(row)


def list_for(session: Session, requester: str | None, status: str | None) -> list[AccessRequestOut]:
    stmt = select(AccessRequest).order_by(AccessRequest.created_at.desc()).limit(200)
    if requester:
        stmt = stmt.where(AccessRequest.requester == requester)
    if status:
        stmt = stmt.where(AccessRequest.status == status)
    return [_out(r) for r in session.scalars(stmt)]


def decide(session: Session, request_id: uuid.UUID, body: DecisionIn) -> AccessRequestOut:
    row = session.get(AccessRequest, request_id, with_for_update=True)
    if row is None:
        raise repo.NotFound(f"access request {request_id}")
    if row.status != "pending_approval":
        raise AccessRuleViolation(f"request is already {row.status}")
    if body.decided_by == row.requester:
        raise AccessRuleViolation("requesters cannot decide their own access request (four-eyes rule)")
    row.status, row.decided_by, row.decision_note = body.decision, body.decided_by, body.note
    row.decided_at = datetime.now(UTC)
    session.flush()
    return _out(row)
