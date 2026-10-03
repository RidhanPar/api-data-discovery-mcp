"""Unit tests for the access policy (pure functions)."""

from __future__ import annotations

from typing import Any

from nordlys_discovery.access.policy import (
    AccessRequirements,
    api_requirements,
    data_product_requirements,
    decide,
    default_scope,
)


def _api(**over: Any) -> dict[str, Any]:
    base = {
        "api_id": "claims-api",
        "major_version": 2,
        "owner_team": "claims-platform",
        "audience": "internal",
        "data_classification": "personal",
        "scopes": ["claims.read", "claims.write"],
        "security_schemes": {"oauth2": "oauth2"},
        "lifecycle_status": "active",
        "sunset": None,
        "replacement": None,
    }
    return base | over


def _dp(**access_over: Any) -> dict[str, Any]:
    access = {
        "scope": "dataproduct.fraud-scores.read",
        "approval": "owner_and_dpo_approval",
        "allowed_purposes": ["fraud_investigation"],
        "prohibited_purposes": ["marketing"],
    } | access_over
    return {
        "product_id": "claims-fraud-scores",
        "owner_team": "fraud-analytics",
        "pii_classification": "sensitive",
        "contract": {"access": access},
    }


def test_internal_api_needs_owner_approval() -> None:
    r = api_requirements(_api())
    assert r.approval_route == "owner_approval" and r.approvers == ["team:claims-platform"]


def test_restricted_and_sensitive_apis_need_security_approval() -> None:
    for over in ({"audience": "restricted"}, {"data_classification": "sensitive"}):
        r = api_requirements(_api(**over))
        assert r.approval_route == "owner_and_security_approval"
        assert "role:information-security" in r.approvers


def test_channel_bff_is_not_requestable() -> None:
    d = decide(api_requirements(_api(audience="channel-bff")), [], None)
    assert not d.can_request and "not offered for reuse" in d.explanation


def test_deprecated_api_carries_a_note() -> None:
    r = api_requirements(_api(lifecycle_status="deprecated", sunset="2026-12-31", replacement="claims-api v2"))
    assert any("claims-api v2" in n for n in r.notes)


def test_holding_one_api_scope_counts_as_access() -> None:
    d = decide(api_requirements(_api()), ["claims.read"], None)
    assert d.has_access and d.missing_scopes == ["claims.write"] and not d.can_request


def test_no_scopes_means_no_access_and_request_possible() -> None:
    d = decide(api_requirements(_api()), [], None)
    assert not d.has_access and d.can_request


def test_sensitive_data_product_needs_owner_and_dpo() -> None:
    r = data_product_requirements(_dp())
    assert r.approval_route == "owner_and_dpo_approval"
    assert r.approvers == ["team:fraud-analytics", "role:data-protection-officer"]


def test_prohibited_purpose_blocks_request() -> None:
    d = decide(data_product_requirements(_dp()), [], "marketing")
    assert d.purpose_allowed is False and not d.can_request


def test_unlisted_purpose_blocks_request() -> None:
    d = decide(data_product_requirements(_dp()), [], "curiosity")
    assert d.purpose_allowed is False and not d.can_request


def test_allowed_purpose_can_request() -> None:
    d = decide(data_product_requirements(_dp()), [], "fraud_investigation")
    assert d.purpose_allowed is True and d.can_request


def test_default_scope_is_least_privilege() -> None:
    assert default_scope(api_requirements(_api())) == "claims.read"
    req = AccessRequirements(
        asset_type="api",
        asset_id="x",
        major_version=1,
        owner_team="t",
        required_scopes=["payout.approve", "payout.read", "payout.request"],
        approval_route="owner_approval",
        approvers=["team:t"],
        classification=None,
    )
    assert default_scope(req) == "payout.read"


def test_decide_never_grants() -> None:
    """There is no code path from 'not has_access' to 'has_access' other than holding the scope."""
    for scopes in ([], ["unrelated.scope"]):
        assert not decide(data_product_requirements(_dp()), scopes, "fraud_investigation").has_access
