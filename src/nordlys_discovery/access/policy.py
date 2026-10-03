"""Access requirements and decisions, as pure functions (no I/O).

Two kinds of permission are involved, and they are easy to confuse:

* Tool scopes (catalog.read, dataproduct.read, access.request) - may the caller use
  the discovery platform itself? Enforced on the MCP server (Phase 4).
* Asset scopes (claims.search, dataproduct.claims-open-cases.read, ...) - may the
  caller use the underlying API or data? Those are what `check_access` reports on
  and what `request_access` asks a human to grant.

The platform never grants an asset scope. It tells you which one you need and
files a request that a named human approver must decide.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Literal

from pydantic import BaseModel

ApprovalRoute = Literal[
    "self_service",
    "owner_approval",
    "owner_and_dpo_approval",
    "owner_and_security_approval",
    "partner_onboarding",
    "not_available_for_reuse",
]

# Human-readable description of each route. Used verbatim in tool output.
ROUTE_EXPLANATION: dict[str, str] = {
    "self_service": "Request is logged and approved by the platform team without a business review.",
    "owner_approval": "The owning team reviews the request and purpose.",
    "owner_and_dpo_approval": "The owning team AND the Data Protection Officer must both approve.",
    "owner_and_security_approval": "The owning team AND Information Security must both approve.",
    "partner_onboarding": (
        "External partner access: contract, partner certificate (mTLS) and onboarding by the partner team."
    ),
    "not_available_for_reuse": (
        "This API serves one channel only and is not offered for reuse; use the recommended alternative."
    ),
}


class AccessRequirements(BaseModel):
    asset_type: Literal["api", "data_product"]
    asset_id: str
    major_version: int | None
    owner_team: str
    required_scopes: list[str]
    approval_route: ApprovalRoute
    approvers: list[str]
    allowed_purposes: list[str] | None = None  # data products only
    prohibited_purposes: list[str] = []
    classification: str | None
    notes: list[str] = []


class AccessDecision(BaseModel):
    requirements: AccessRequirements
    has_access: bool
    missing_scopes: list[str]
    purpose: str | None
    purpose_allowed: bool | None  # None = no purpose given / not applicable
    can_request: bool
    explanation: str
    next_step: str


def api_requirements(details: dict[str, Any]) -> AccessRequirements:
    """Requirements for an API version, from the catalog service's ApiDetails payload."""
    audience = details.get("audience", "internal")
    classification = details.get("data_classification")
    scopes: list[str] = sorted(details.get("scopes", []))
    owner = details["owner_team"]
    notes: list[str] = []
    approvers = [f"team:{owner}"]
    route: ApprovalRoute = "owner_approval"
    if audience == "restricted":
        route = "owner_and_security_approval"
        approvers.append("role:information-security")
    elif audience == "partner":
        route = "partner_onboarding"
        approvers.append("team:partner-integrations")
    elif classification == "sensitive":
        route = "owner_and_security_approval"
        approvers.append("role:information-security")
    elif audience == "channel-bff":
        route = "not_available_for_reuse"
        notes.append("Backend-for-frontend of a customer channel; it only returns the signed-in customer's own data.")
    if "apiKey" in details.get("security_schemes", {}).values():
        notes.append("Legacy API-key authentication: keys are issued by the owning team, not via OAuth scopes.")
    if details.get("lifecycle_status") == "deprecated":
        notes.append(
            f"Deprecated: sunset {details.get('sunset')}. New consumers should use {details.get('replacement')}."
        )
    return AccessRequirements(
        asset_type="api",
        asset_id=details["api_id"],
        major_version=details["major_version"],
        owner_team=owner,
        required_scopes=scopes,
        approval_route=route,
        approvers=approvers,
        classification=classification,
        notes=notes,
    )


def data_product_requirements(product: dict[str, Any]) -> AccessRequirements:
    """Requirements for a data product, from the catalog service's DataProductDetails payload."""
    access = product["contract"]["access"]
    route: ApprovalRoute = access["approval"]
    approvers = [f"team:{product['owner_team']}"]
    if route == "owner_and_dpo_approval":
        approvers.append("role:data-protection-officer")
    notes = []
    if product["pii_classification"] == "sensitive":
        notes.append("Sensitive data: only metadata is shown in discovery; sample data is never displayed.")
    return AccessRequirements(
        asset_type="data_product",
        asset_id=product["product_id"],
        major_version=None,
        owner_team=product["owner_team"],
        required_scopes=[access["scope"]],
        approval_route=route,
        approvers=approvers,
        allowed_purposes=list(access["allowed_purposes"]),
        prohibited_purposes=list(access.get("prohibited_purposes", [])),
        classification=product["pii_classification"],
        notes=notes,
    )


def default_scope(req: AccessRequirements) -> str | None:
    """Least-privilege scope to request when the caller does not name one."""
    for word in ("read", "search"):
        for s in req.required_scopes:
            if word in s.split(".")[-1] or s.endswith(f".{word}.self"):
                return s
    return req.required_scopes[0] if req.required_scopes else None


def decide(req: AccessRequirements, caller_scopes: Iterable[str], purpose: str | None) -> AccessDecision:
    held = set(caller_scopes)
    # APIs: any one listed scope gives *some* access; we report the full set needed for all endpoints.
    missing = sorted(set(req.required_scopes) - held)
    has_access = bool(req.required_scopes) and (
        not missing if req.asset_type == "data_product" else bool(held & set(req.required_scopes))
    )

    purpose_allowed: bool | None = None
    if purpose is not None and req.asset_type == "data_product":
        purpose_allowed = purpose in (req.allowed_purposes or []) and purpose not in req.prohibited_purposes

    can_request = (
        not has_access
        and req.approval_route != "not_available_for_reuse"
        and purpose_allowed is not False
        and bool(req.required_scopes)
    )

    if req.approval_route == "not_available_for_reuse":
        explanation = ROUTE_EXPLANATION["not_available_for_reuse"]
        next_step = "Search the catalog for the system-of-record API instead."
    elif has_access:
        explanation = "You already hold a scope that grants access."
        next_step = "Call the API with a token carrying the scope(s) listed."
    elif purpose_allowed is False:
        explanation = (
            f"Purpose '{purpose}' is not permitted for this data product. "
            f"Allowed: {', '.join(req.allowed_purposes or [])}."
        )
        next_step = "Choose an allowed purpose, or talk to the owning team if none fits. No request was created."
    elif not req.required_scopes:
        explanation = "No OAuth scope is defined for this asset (legacy authentication)."
        next_step = f"Contact team {req.owner_team} directly."
    else:
        explanation = f"Missing scope(s): {', '.join(missing)}. {ROUTE_EXPLANATION[req.approval_route]}"
        next_step = (
            "Call request_access with a purpose and justification. The request stays pending until "
            f"{' and '.join(req.approvers)} approve it."
        )
    return AccessDecision(
        requirements=req,
        has_access=has_access,
        missing_scopes=missing,
        purpose=purpose,
        purpose_allowed=purpose_allowed,
        can_request=can_request,
        explanation=explanation,
        next_step=next_step,
    )
