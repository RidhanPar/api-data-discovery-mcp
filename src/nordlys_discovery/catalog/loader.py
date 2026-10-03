"""Load and validate the catalog files from disk.

Used by the validation CLI (Phase 1) and by ingestion (Phase 2).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from openapi_spec_validator import validate
from openapi_spec_validator.validation.exceptions import OpenAPIValidationError

from .models import DataProduct

DEFAULT_CATALOG_DIR = Path(__file__).resolve().parents[3] / "catalog"


class CatalogError(Exception):
    """A catalog file is unreadable or invalid."""

    def __init__(self, path: Path, message: str) -> None:
        super().__init__(f"{path}: {message}")
        self.path = path


@dataclass(frozen=True)
class ApiSpecFile:
    path: Path
    document: dict[str, Any]

    @property
    def title(self) -> str:
        return str(self.document["info"]["title"])

    @property
    def version(self) -> str:
        return str(self.document["info"]["version"])


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CatalogError(path, f"invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise CatalogError(path, "top level must be a mapping")
    return data


def load_api_spec(path: Path) -> ApiSpecFile:
    doc = _read_yaml(path)
    if not str(doc.get("openapi", "")).startswith("3.1"):
        raise CatalogError(path, f"expected OpenAPI 3.1.x, got {doc.get('openapi')!r}")
    try:
        validate(doc)
    except OpenAPIValidationError as exc:
        raise CatalogError(path, f"OpenAPI validation failed: {exc.message}") from exc
    return ApiSpecFile(path=path, document=doc)


def load_data_product(path: Path) -> DataProduct:
    try:
        return DataProduct.model_validate(_read_yaml(path))
    except ValueError as exc:  # pydantic.ValidationError subclasses ValueError
        raise CatalogError(path, str(exc)) from exc


def load_catalog(root: Path = DEFAULT_CATALOG_DIR) -> tuple[list[ApiSpecFile], list[DataProduct]]:
    specs = [load_api_spec(p) for p in sorted((root / "apis").rglob("*.yaml"))]
    products = [load_data_product(p) for p in sorted((root / "data-products").glob("*.yaml"))]
    return specs, products
