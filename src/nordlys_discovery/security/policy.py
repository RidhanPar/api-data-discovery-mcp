"""Security policy as code: which scope each tool needs, and who may see sample data.

Everything here is a pure function or a constant, so the policy can be reviewed in one
file and exhaustively unit-tested (tests/test_security_policy.py).
"""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel

# --- tool scopes: may the caller use this tool at all? ------------------------------
TOOL_SCOPES: dict[str, str] = {
    "search_catalog": "catalog.read",
    "get_api_details": "catalog.read",
    "get_endpoint_schema": "catalog.read",
    "compare_api_versions": "catalog.read",
    "list_deprecations": "catalog.read",
    "check_access": "catalog.read",
    "get_data_product": "dataproduct.read",
    "request_access": "access.request",
}
# Reading resources (raw specs, contracts) needs the same scope as the matching tools.
RESOURCE_SCOPE = "catalog.read"
PLATFORM_SCOPES = frozenset({*TOOL_SCOPES.values(), "access.approve", "catalog.internal"})


class PolicyDecision(BaseModel):
    allowed: bool
    reason: str


def tool_decision(tool: str, scopes: Iterable[str]) -> PolicyDecision:
    required = TOOL_SCOPES.get(tool)
    if required is None:
        # Unknown tools are denied: a newly added tool must be given a scope here first.
        return PolicyDecision(allowed=False, reason=f"tool '{tool}' has no scope mapping (deny by default)")
    if required in set(scopes):
        return PolicyDecision(allowed=True, reason=f"scope {required} present")
    return PolicyDecision(allowed=False, reason=f"missing scope {required}")


def asset_scopes(scopes: Iterable[str]) -> frozenset[str]:
    """Scopes that grant access to underlying APIs/data, i.e. everything but platform scopes."""
    return frozenset(s for s in scopes if s not in PLATFORM_SCOPES and s not in {"openid", "profile", "email"})


# --- sample data -----------------------------------------------------------------------


def sample_rows_decision(pii_classification: str, product_scope: str, scopes: Iterable[str]) -> PolicyDecision:
    """May this caller see example rows of a data product?

    none / internal     -> any caller with dataproduct.read
    personal / sensitive -> only a caller who already holds the product's own access scope,
                           i.e. someone already approved to read the real data.
    Discovery must never become a side door to personal or sensitive data.
    """
    held = set(scopes)
    if "dataproduct.read" not in held:
        return PolicyDecision(allowed=False, reason="missing scope dataproduct.read")
    if pii_classification in {"none", "internal"}:
        return PolicyDecision(allowed=True, reason=f"{pii_classification} data: dataproduct.read is sufficient")
    if pii_classification in {"personal", "sensitive"}:
        if product_scope in held:
            return PolicyDecision(allowed=True, reason=f"caller holds the product scope {product_scope}")
        return PolicyDecision(
            allowed=False,
            reason=f"{pii_classification} data: metadata only; sample rows need {product_scope}",
        )
    return PolicyDecision(allowed=False, reason=f"unknown classification '{pii_classification}' (deny by default)")
