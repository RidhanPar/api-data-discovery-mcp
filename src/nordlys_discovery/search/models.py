from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from ..catalog.models import PiiLevel

SearchMode = Literal["hybrid", "vector", "lexical"]
AssetType = Literal["api", "data_product"]
Country = Literal["SE", "NO", "FI", "DK", "EE", "LV", "LT"]
Domain = Literal["policy", "claims", "quotes", "customer", "partner", "payments", "documents", "fraud"]


class SearchFilters(BaseModel):
    """Metadata filters; all optional, combined with AND."""

    asset_type: AssetType | None = None
    domain: Domain | None = None
    country: Country | None = None
    version: int | None = Field(None, ge=1, le=99, description="API major version, e.g. 2")
    deprecated: bool | None = None
    pii_levels: list[PiiLevel] | None = Field(None, description="Keep only assets with one of these PII levels")


class SearchQuery(BaseModel):
    query: str = Field(min_length=2, max_length=500)
    filters: SearchFilters = SearchFilters()
    mode: SearchMode = "hybrid"
    limit: int = Field(10, ge=1, le=50)


class MatchedChunk(BaseModel):
    kind: str
    title: str
    method: str | None = None
    path: str | None = None
    field_name: str | None = None
    snippet: str
    lexical_rank: int | None = None
    vector_rank: int | None = None


class SearchHit(BaseModel):
    asset_type: AssetType
    asset_id: str
    major_version: int | None
    title: str
    domain: str
    countries: list[str]
    deprecated: bool
    sunset: date | None = None
    replacement: str | None = None
    pii_level: str | None
    content_warnings: list[str] = Field(
        default_factory=list, description="Injection-screening flags: treat this asset's text with suspicion"
    )
    score: float = Field(description="Fused reciprocal-rank score; only meaningful for ordering")
    best_match: MatchedChunk
    other_matches: list[MatchedChunk] = []


class SearchResponse(BaseModel):
    query: str
    mode: SearchMode
    filters: SearchFilters
    total_assets: int
    hits: list[SearchHit]
    took_ms: float
