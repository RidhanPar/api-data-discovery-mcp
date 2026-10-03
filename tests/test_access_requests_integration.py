"""Integration tests for the access-request workflow (catalog service + Postgres)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from nordlys_discovery.catalog.loader import DEFAULT_CATALOG_DIR
from nordlys_discovery.db.models import AccessRequest
from nordlys_discovery.embeddings.others import HashingEmbeddings
from nordlys_discovery.ingest.pipeline import ingest_catalog
from nordlys_discovery.service import access

pytestmark = pytest.mark.integration


@pytest.fixture
def db(session: Session) -> Session:
    ingest_catalog(session, DEFAULT_CATALOG_DIR, HashingEmbeddings(384))
    session.flush()
    return session


def _req(**over: object) -> access.AccessRequestIn:
    base = dict(
        requester="alice@nordlys.example",
        asset_type="data_product",
        asset_id="claims-open-cases-daily",
        purpose="claims_operations_reporting",
        justification="Backlog dashboard for the Norwegian claims team.",
    )
    return access.AccessRequestIn.model_validate(base | over)


def test_request_is_created_pending(db: Session) -> None:
    out = access.create(db, _req())
    assert out.status == "pending_approval" and out.created
    assert out.requested_scope == "dataproduct.claims-open-cases.read"
    assert out.approvers == ["team:claims-platform"]


def test_identical_open_request_is_not_duplicated(db: Session) -> None:
    first = access.create(db, _req())
    second = access.create(db, _req())
    assert second.id == first.id and second.created is False
    assert db.scalar(select(func.count()).select_from(AccessRequest)) == 1


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"purpose": "marketing"}, "prohibited"),
        ({"purpose": "curiosity_driven"}, "not an allowed purpose"),
        ({"asset_type": "api", "asset_id": "claim-lookup-api", "version": 1}, "not offered for reuse"),
        ({"asset_type": "api", "asset_id": "claims-api", "version": 2, "scope": "admin.everything"}, "does not belong"),
        ({"asset_type": "api", "asset_id": "claims-api"}, "version is required"),
    ],
)
def test_rule_violations_create_nothing(db: Session, over: dict[str, object], message: str) -> None:
    with pytest.raises(access.AccessRuleViolation, match=message):
        access.create(db, _req(**over))
    assert db.scalar(select(func.count()).select_from(AccessRequest)) == 0


def test_sensitive_product_routes_to_dpo(db: Session) -> None:
    out = access.create(db, _req(asset_id="health-claims-diagnosis", purpose="medical_claims_assessment"))
    assert out.approval_route == "owner_and_dpo_approval"
    assert "role:data-protection-officer" in out.approvers


def test_human_decision_with_four_eyes(db: Session) -> None:
    out = access.create(db, _req())
    with pytest.raises(access.AccessRuleViolation, match="four-eyes"):
        access.decide(
            db, out.id, access.DecisionIn(decided_by="alice@nordlys.example", decision="approved", note="self-approve")
        )
    decided = access.decide(
        db, out.id, access.DecisionIn(decided_by="bob@nordlys.example", decision="approved", note="ok for BAU")
    )
    assert decided.status == "approved" and decided.decided_by == "bob@nordlys.example"
    with pytest.raises(access.AccessRuleViolation, match="already approved"):
        access.decide(
            db, out.id, access.DecisionIn(decided_by="carol@nordlys.example", decision="rejected", note="not needed")
        )


def test_database_enforces_four_eyes_even_if_code_is_bypassed(db: Session) -> None:
    out = access.create(db, _req())
    db.commit()
    with pytest.raises(IntegrityError, match="ck_access_request_four_eyes"):
        db.execute(
            text("UPDATE access_request SET status='approved', decided_by=requester WHERE id=:id"), {"id": out.id}
        )
    db.rollback()


def test_database_rejects_unknown_status(db: Session) -> None:
    out = access.create(db, _req())
    db.commit()
    with pytest.raises(IntegrityError, match="ck_access_request_status"):
        db.execute(
            text("UPDATE access_request SET status='auto_granted', decided_by='system' WHERE id=:id"), {"id": out.id}
        )
    db.rollback()


def test_unknown_request_is_not_found(db: Session) -> None:
    from nordlys_discovery.service.repository import NotFound

    with pytest.raises(NotFound):
        access.get(db, uuid.uuid4())
