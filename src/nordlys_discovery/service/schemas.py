"""Response models for the catalog service (also the contract the MCP server consumes)."""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel

from ..catalog.diff import VersionDiff


class ApiSummary(BaseModel):
    api_id: str
    major_version: int
    version: str
    title: str
    domain: str
    owner_team: str
    countries: list[str]
    audience: str
    lifecycle_status: str
    sunset: date | None
    replacement: str | None


class EndpointSummary(BaseModel):
    method: str
    path: str
    summary: str | None
    operation_id: str | None
    deprecated: bool
    scopes: list[str]


class DocQuality(BaseModel):
    description_coverage: float
    has_api_description: bool
    metadata_provenance: dict[str, str]
    content_warnings: list[str] = []


class ApiDetails(ApiSummary):
    description: str | None
    data_classification: str | None
    servers: list[str]
    security_schemes: dict[str, str]
    scopes: list[str]
    endpoints: list[EndpointSummary]
    documentation_quality: DocQuality
    other_versions: list[int]


class EndpointSchema(BaseModel):
    api_id: str
    major_version: int
    method: str
    path: str
    summary: str | None
    description: str | None
    deprecated: bool
    scopes: list[str]
    servers: list[str]
    parameters: list[dict[str, Any]]
    request_body: dict[str, Any] | None
    responses: dict[str, Any]


class VersionComparison(BaseModel):
    api_id: str
    from_version: ApiSummary
    to_version: ApiSummary
    is_breaking: bool
    diff: VersionDiff


class Deprecation(ApiSummary):
    days_until_sunset: int | None
    past_sunset: bool


class DataProductSummary(BaseModel):
    product_id: str
    name: str
    version: str
    status: str
    domain: str
    owner_team: str
    countries: list[str]
    pii_classification: str


class DataProductDetails(DataProductSummary):
    description: str
    content_warnings: list[str] = []
    contract: dict[str, Any]
    sample_rows: list[dict[str, Any]] | None
    sample_rows_withheld: bool
    sample_policy_reason: str | None = None


class SampleRows(BaseModel):
    product_id: str
    rows: list[dict[str, Any]]
    policy_reason: str


class Problem(BaseModel):
    """RFC 9457 problem details."""

    type: str = "about:blank"
    title: str
    status: int
    detail: str | None = None
    instance: str | None = None
    request_id: str | None = None
