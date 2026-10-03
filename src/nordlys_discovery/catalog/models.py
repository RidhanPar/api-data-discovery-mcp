"""Typed model for Data Product descriptors (data-contract style YAML).

Loosely follows the Open Data Contract Standard (ODCS): a contract states what the
data is, who owns it, how fresh and how good it is, how sensitive it is, and who
may use it for which purpose.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class PiiLevel(StrEnum):
    """Ordered from least to most sensitive. Ordering matters for policy checks."""

    NONE = "none"
    INTERNAL = "internal"
    PERSONAL = "personal"
    SENSITIVE = "sensitive"

    @property
    def rank(self) -> int:
        return list(PiiLevel).index(self)


Country = Literal["SE", "NO", "FI", "DK", "EE", "LV", "LT"]
Domain = Literal["policy", "claims", "quotes", "customer", "partner", "payments", "documents", "fraud"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Owner(_Strict):
    team: str
    product_owner: str = Field(description="Role or alias, never a real person's name.")
    contact: str


class Field_(_Strict):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    type: Literal["string", "integer", "decimal", "boolean", "date", "timestamp", "array", "object"]
    description: str
    pii: PiiLevel = PiiLevel.NONE
    nullable: bool = False
    primary_key: bool = False
    example: str | int | float | bool | None = None


class Freshness(_Strict):
    update_frequency: Literal["streaming", "hourly", "daily", "weekly", "monthly"]
    max_delay_hours: float = Field(gt=0)
    sla: str = Field(description="Human-readable promise, e.g. 'Available by 06:00 CET'.")


class QualityCheck(_Strict):
    name: str
    type: Literal[
        "not_null",
        "unique",
        "accepted_values",
        "range",
        "row_count",
        "freshness",
        "referential_integrity",
        "custom_sql",
    ]
    column: str | None = None
    rule: str
    severity: Literal["warn", "error"] = "error"


class OutputPort(_Strict):
    type: Literal["databricks_table", "postgres_view", "parquet_adls", "kafka_topic", "rest_api"]
    location: str


class Access(_Strict):
    scope: str = Field(description="OAuth scope or entitlement needed to read the data.")
    approval: Literal["self_service", "owner_approval", "owner_and_dpo_approval"]
    allowed_purposes: list[str] = Field(min_length=1)
    prohibited_purposes: list[str] = []
    retention: str


class DataProduct(_Strict):
    api_version: Literal["nordlys.io/data-product/v1"] = Field(alias="apiVersion")
    kind: Literal["DataProduct"]
    id: str = Field(pattern=r"^[a-z][a-z0-9-]+$")
    name: str
    version: str
    status: Literal["active", "deprecated", "draft"] = "active"
    domain: Domain
    description: str
    owner: Owner
    countries: list[Country] = Field(min_length=1)
    pii_classification: PiiLevel
    tags: list[str] = []
    freshness: Freshness
    schema_: list[Field_] = Field(alias="schema", min_length=1)
    quality_checks: list[QualityCheck] = Field(min_length=1)
    access: Access
    output_ports: list[OutputPort] = Field(min_length=1)
    sample_rows: list[dict[str, str | int | float | bool | None]] = []
    related_apis: list[str] = []
    last_reviewed: date

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    @field_validator("schema_")
    @classmethod
    def _unique_field_names(cls, fields: list[Field_]) -> list[Field_]:
        names = [f.name for f in fields]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"duplicate field names: {sorted(dupes)}")
        return fields

    @model_validator(mode="after")
    def _consistency(self) -> DataProduct:
        # The product's classification must be at least as strict as its most sensitive field.
        worst = max((f.pii for f in self.schema_), key=lambda p: p.rank)
        if worst.rank > self.pii_classification.rank:
            raise ValueError(f"pii_classification '{self.pii_classification}' is weaker than field level '{worst}'")
        columns = {f.name for f in self.schema_}
        for check in self.quality_checks:
            if check.column and check.column not in columns:
                raise ValueError(f"quality check '{check.name}' references unknown column '{check.column}'")
        for row in self.sample_rows:
            unknown = set(row) - columns
            if unknown:
                raise ValueError(f"sample row has unknown columns {sorted(unknown)}")
        if self.pii_classification is PiiLevel.SENSITIVE and self.access.approval != "owner_and_dpo_approval":
            raise ValueError("sensitive data products require owner_and_dpo_approval")
        return self
