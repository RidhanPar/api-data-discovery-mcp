"""Tool input types and output models for the MCP server.

Inputs are constrained at the schema level (patterns, enums, ranges), so a model
sees the rules in the tool's JSON Schema and bad arguments are rejected before any
tool code runs.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from ..access.policy import AccessDecision
from ..service.access import AccessRequestOut
from ..service.schemas import Deprecation

AssetId = Annotated[
    str,
    Field(
        pattern=r"^[a-z][a-z0-9-]{1,119}$",
        description="Catalog id in kebab-case, e.g. 'claims-search-api' or 'claims-open-cases-daily'.",
    ),
]
MajorVersion = Annotated[int, Field(ge=1, le=99, description="API major version, e.g. 2 for v2.")]
AssetType = Literal["api", "data_product"]
Domain = Literal["policy", "claims", "quotes", "customer", "partner", "payments", "documents", "fraud"]
Country = Literal["SE", "NO", "FI", "DK", "EE", "LV", "LT"]
PiiLevel = Literal["none", "internal", "personal", "sensitive"]
HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
ApiPath = Annotated[
    str,
    Field(
        pattern=r"^/[A-Za-z0-9/{}_.:\-]{0,299}$",
        description="Path template exactly as listed by get_api_details, e.g. '/claims/{claimId}'.",
    ),
]
Purpose = Annotated[
    str,
    Field(
        pattern=r"^[a-z][a-z0-9_]{2,60}$",
        description="Purpose label in snake_case. For data products it must be one of the contract's allowed purposes.",
    ),
]


Query = Annotated[str, Field(min_length=2, max_length=500, description="Natural-language question or keywords.")]
Limit = Annotated[int, Field(ge=1, le=20, description="Maximum number of assets to return (1-20).")]
Justification = Annotated[
    str, Field(min_length=20, max_length=2000, description="Business need for the access, 20-2000 characters.")
]
Scope = Annotated[str, Field(pattern=r"^[a-z][a-z0-9.\-]{2,199}$", description="OAuth scope to request.")]


class MatchSummary(BaseModel):
    kind: str
    title: str
    method: str | None = None
    path: str | None = None
    field_name: str | None = None
    snippet: str


class SearchResultItem(BaseModel):
    rank: int
    citation: str = Field(description="Stable reference to quote in answers, e.g. 'claims-search-api v1 GET /claims'.")
    asset_type: AssetType
    asset_id: str
    version: int | None
    title: str
    domain: str
    countries: list[str]
    deprecated: bool
    sunset: date | None
    replacement: str | None
    pii_level: str | None
    best_match: MatchSummary
    other_matches: list[str]


class SearchResults(BaseModel):
    query: str
    filters_applied: dict[str, object]
    total_assets_matched: int
    results: list[SearchResultItem]
    guidance: str


class AccessCheck(BaseModel):
    caller: str
    decision: AccessDecision


class AccessRequestResult(BaseModel):
    outcome: Literal["created", "already_pending", "not_needed"]
    message: str
    request: AccessRequestOut | None


class DeprecationList(BaseModel):
    as_of: date
    deprecations: list[Deprecation]
