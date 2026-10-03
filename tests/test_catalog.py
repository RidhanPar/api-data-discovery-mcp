"""Phase 1 contract tests for the synthetic catalog.

These pin down the catalog's deliberate properties (deprecations, near-duplicates,
the undocumented spec...) because later phases - search quality evals in particular -
depend on them.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from nordlys_discovery.catalog.loader import (
    DEFAULT_CATALOG_DIR,
    CatalogError,
    load_api_spec,
    load_catalog,
    load_data_product,
)
from nordlys_discovery.catalog.metadata import (
    ApiMetadata,
    description_coverage,
    extract_metadata,
    iter_operations,
)
from nordlys_discovery.catalog.models import DataProduct, PiiLevel
from scripts.catalog_gen.__main__ import ALL_SPECS, OUT_DIR, render


@pytest.fixture(scope="module")
def catalog() -> tuple[list[Any], list[DataProduct]]:
    return load_catalog()


@pytest.fixture(scope="module")
def metas(catalog: tuple[list[Any], list[DataProduct]]) -> list[ApiMetadata]:
    specs, _ = catalog
    return [extract_metadata(s.document, s.path) for s in specs]


# --------------------------------------------------------------------------- generator


def test_committed_specs_match_generator() -> None:
    for rel_path, build in ALL_SPECS:
        target = OUT_DIR / rel_path
        assert target.read_text(encoding="utf-8") == render(build()), (
            f"{rel_path} drifted; run `uv run python -m scripts.catalog_gen`"
        )


# --------------------------------------------------------------------------- shape


def test_catalog_size(catalog: tuple[list[Any], list[DataProduct]]) -> None:
    specs, products = catalog
    assert len(specs) == 25
    assert len(products) == 15


def test_all_specs_are_openapi_31(catalog: tuple[list[Any], list[DataProduct]]) -> None:
    specs, _ = catalog
    assert all(s.document["openapi"].startswith("3.1") for s in specs)


def test_all_domains_and_markets_covered(metas: list[ApiMetadata]) -> None:
    assert {m.domain for m in metas} == {
        "policy",
        "claims",
        "quotes",
        "customer",
        "partner",
        "payments",
        "documents",
        "fraud",
    }
    covered = {c for m in metas for c in m.countries}
    assert covered == {"SE", "NO", "FI", "DK", "EE", "LV", "LT"}


def test_multiple_major_versions_exist(metas: list[ApiMetadata]) -> None:
    versions: dict[str, set[int]] = {}
    for m in metas:
        versions.setdefault(m.api_id, set()).add(m.major_version)
    multi = {api for api, v in versions.items() if {1, 2} <= v}
    assert multi == {"policy-api", "claims-api", "quote-api"}


def test_api_ids_and_versions_are_unique(metas: list[ApiMetadata]) -> None:
    keys = [(m.api_id, m.major_version) for m in metas]
    assert len(keys) == len(set(keys))


# --------------------------------------------------------------------------- deliberate realism


def test_exactly_three_deprecated_apis_with_sunset_and_replacement(metas: list[ApiMetadata]) -> None:
    deprecated = [m for m in metas if m.deprecated]
    assert len(deprecated) == 3
    existing = {f"{m.api_id} v{m.major_version}" for m in metas if not m.deprecated}
    for m in deprecated:
        assert m.sunset is not None
        assert m.replacement in existing, f"{m.api_id} points to missing replacement {m.replacement}"


def test_deprecated_apis_flag_every_operation(
    catalog: tuple[list[Any], list[DataProduct]], metas: list[ApiMetadata]
) -> None:
    specs, _ = catalog
    for spec, m in zip(specs, metas, strict=True):
        flags = [op.get("deprecated", False) for _, _, op in iter_operations(spec.document)]
        assert all(flags) if m.deprecated else not any(flags), spec.path.name


def test_near_duplicate_claim_listing_apis(
    metas: list[ApiMetadata], catalog: tuple[list[Any], list[DataProduct]]
) -> None:
    """Two claims APIs both list claims filtered by status + market, owned by different teams."""
    specs, _ = catalog
    by_id = {m.api_id: (s, m) for s, m in zip(specs, metas, strict=True)}
    search_spec, search = by_id["claims-search-api"]
    lookup_spec, lookup = by_id["claim-lookup-api"]
    assert search.domain == lookup.domain == "claims"
    assert search.owner_team != lookup.owner_team

    def list_params(doc: dict[str, Any]) -> set[str]:
        return {
            p["name"] for _, method, op in iter_operations(doc) if method == "GET" for p in op.get("parameters", [])
        }

    assert {"status", "country"} <= list_params(search_spec.document)
    assert {"state", "market"} <= list_params(lookup_spec.document)


def test_exactly_one_spec_without_descriptions(catalog: tuple[list[Any], list[DataProduct]]) -> None:
    specs, _ = catalog
    undocumented = [s.path.name for s in specs if description_coverage(s.document) == 0.0]
    assert undocumented == ["dk-document-archive-api.v1.yaml"]
    others = [s for s in specs if s.path.name not in undocumented]
    assert all(description_coverage(s.document) == 1.0 for s in others)


def test_legacy_metadata_is_normalised_with_provenance(metas: list[ApiMetadata]) -> None:
    by_id = {m.api_id: m for m in metas}
    motor = by_id["motor-policy-api"]
    assert motor.domain == "policy"
    assert motor.countries == ("EE", "LV", "LT")
    assert motor.provenance["countries"] == "legacy-extension"

    archive = by_id["dk-document-archive-api"]
    assert archive.domain == "documents"
    assert archive.countries == ("DK",)
    assert archive.provenance["countries"] == "inferred:server-url"


def test_descriptions_are_inconsistent_across_teams(catalog: tuple[list[Any], list[DataProduct]]) -> None:
    """Some teams write sentence-case summaries, some lower-case shorthand."""
    specs, _ = catalog
    summaries = [op["summary"] for s in specs for _, _, op in iter_operations(s.document) if "summary" in op]
    assert any(s[0].islower() for s in summaries)
    assert any(s[0].isupper() for s in summaries)


# --------------------------------------------------------------------------- data products


def test_every_pii_level_is_represented(catalog: tuple[list[Any], list[DataProduct]]) -> None:
    _, products = catalog
    assert {p.pii_classification for p in products} == set(PiiLevel)


def test_sensitive_products_need_dpo_approval(catalog: tuple[list[Any], list[DataProduct]]) -> None:
    _, products = catalog
    sensitive = [p for p in products if p.pii_classification is PiiLevel.SENSITIVE]
    assert {p.id for p in sensitive} == {"claims-fraud-scores", "health-claims-diagnosis"}
    assert all(p.access.approval == "owner_and_dpo_approval" for p in sensitive)


def test_related_apis_exist(catalog: tuple[list[Any], list[DataProduct]], metas: list[ApiMetadata]) -> None:
    _, products = catalog
    api_ids = {m.api_id for m in metas}
    for p in products:
        assert set(p.related_apis) <= api_ids, p.id


def test_product_file_names_match_ids() -> None:
    for path in (DEFAULT_CATALOG_DIR / "data-products").glob("*.yaml"):
        assert load_data_product(path).id == path.stem


# --------------------------------------------------------------------------- fictional brand only

REAL_INSURERS = [
    "gjensidige",
    "tryg",
    "storebrand",
    "fremtind",
    "folksam",
    "länsförsäkringar",
    "trygg-hansa",
    "codan",
    "topdanmark",
    "sampo",
    "fennia",
    "lähitapiola",
    "alm. brand",
    "ergo",
    "if p&c",
]


def test_catalog_uses_only_fictional_brand_and_reserved_domains() -> None:
    for path in DEFAULT_CATALOG_DIR.rglob("*.yaml"):
        text = path.read_text(encoding="utf-8").lower()
        for name in REAL_INSURERS:
            assert not re.search(rf"\b{re.escape(name)}\b", text), f"{path.name} mentions {name}"
        for host in re.findall(r"https?://([^/\s'\"]+)", text):
            assert host.endswith(".example"), f"{path.name} uses non-reserved host {host}"


# --------------------------------------------------------------------------- validators reject bad input


def _valid_spec_doc() -> dict[str, Any]:
    path = DEFAULT_CATALOG_DIR / "apis" / "claims" / "claims-api.v2.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def _write(tmp_path: Path, doc: dict[str, Any], name: str = "x.v1.yaml") -> Path:
    p = tmp_path / name
    p.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return p


def test_loader_rejects_openapi_30(tmp_path: Path) -> None:
    doc = _valid_spec_doc()
    doc["openapi"] = "3.0.3"
    with pytest.raises(CatalogError, match=r"expected OpenAPI 3\.1"):
        load_api_spec(_write(tmp_path, doc))


def test_loader_rejects_spec_missing_required_field(tmp_path: Path) -> None:
    doc = _valid_spec_doc()
    del doc["info"]["title"]
    with pytest.raises(CatalogError, match="OpenAPI validation failed"):
        load_api_spec(_write(tmp_path, doc))


def test_loader_rejects_undeclared_path_parameter(tmp_path: Path) -> None:
    doc = _valid_spec_doc()
    doc["paths"]["/claims/{claimId}"]["get"]["parameters"] = []
    with pytest.raises(CatalogError, match="OpenAPI validation failed"):
        load_api_spec(_write(tmp_path, doc))


def _valid_product_doc() -> dict[str, Any]:
    path = DEFAULT_CATALOG_DIR / "data-products" / "claims-open-cases-daily.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def test_product_classification_cannot_be_weaker_than_fields() -> None:
    doc = _valid_product_doc()
    doc["pii_classification"] = "internal"  # but customer_id is personal
    with pytest.raises(ValueError, match="weaker than field level"):
        DataProduct.model_validate(doc)


def test_sensitive_product_requires_dpo_approval() -> None:
    doc = _valid_product_doc()
    doc["pii_classification"] = "sensitive"
    with pytest.raises(ValueError, match="owner_and_dpo_approval"):
        DataProduct.model_validate(doc)


def test_sample_rows_must_match_schema() -> None:
    doc = copy.deepcopy(_valid_product_doc())
    doc["sample_rows"][0]["national_id"] = "010190-12345"
    with pytest.raises(ValueError, match="unknown columns"):
        DataProduct.model_validate(doc)


def test_unknown_keys_are_rejected() -> None:
    doc = _valid_product_doc()
    doc["ownr"] = "typo"
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        DataProduct.model_validate(doc)
