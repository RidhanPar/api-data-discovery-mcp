"""Unit tests for Phase 2 building blocks (no database needed)."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pytest
import yaml

from nordlys_discovery.catalog.diff import diff_specs
from nordlys_discovery.catalog.loader import DEFAULT_CATALOG_DIR, load_catalog
from nordlys_discovery.catalog.metadata import extract_metadata
from nordlys_discovery.config import Settings
from nordlys_discovery.embeddings import EmbeddingError, create_provider
from nordlys_discovery.embeddings.others import HashingEmbeddings
from nordlys_discovery.ingest.chunking import ChunkDraft, api_chunks, data_product_chunks, render_schema
from nordlys_discovery.search.hybrid import _filter_sql, reciprocal_rank_fusion
from nordlys_discovery.search.models import SearchFilters
from nordlys_discovery.service.repository import dereference

APIS = DEFAULT_CATALOG_DIR / "apis"


def _doc(rel: str) -> dict[str, Any]:
    return yaml.safe_load((APIS / rel).read_text(encoding="utf-8"))  # type: ignore[no-any-return]


# --------------------------------------------------------------------------- RRF


def test_rrf_rewards_agreement_between_retrievers() -> None:
    scores = reciprocal_rank_fusion([[1, 2, 3], [3, 2, 9]], k=60)
    assert scores[2] > scores[1]  # rank 2 in both beats rank 1 in only one
    assert math.isclose(scores[3], 1 / 63 + 1 / 61)
    assert set(scores) == {1, 2, 3, 9}


def test_rrf_handles_empty_lists() -> None:
    assert reciprocal_rank_fusion([[], []]) == {}
    assert reciprocal_rank_fusion([[7], []]) == {7: 1 / 61}


# --------------------------------------------------------------------------- chunking


def test_chunk_counts_match_catalog() -> None:
    specs, products = load_catalog()
    api = [c for s in specs for c in api_chunks(extract_metadata(s.document, s.path), s.document)]
    dp = [c for p in products for c in data_product_chunks(p)]
    assert sum(c.kind == "api" for c in api) == 25
    assert sum(c.kind == "endpoint" for c in api) == 69
    assert sum(c.kind == "data_product" for c in dp) == 15
    assert sum(c.kind == "field" for c in dp) == sum(len(p.schema_) for p in products)
    keys = [c.key for c in api + dp]
    assert len(keys) == len(set(keys)), "chunk keys must be unique"


def _endpoint(rel: str, method: str, path: str) -> ChunkDraft:
    doc = _doc(rel)
    chunks = api_chunks(extract_metadata(doc, APIS / rel), doc)
    return next(c for c in chunks if c.method == method and c.path == path)


def test_endpoint_chunk_carries_context_scopes_and_lifecycle() -> None:
    c = _endpoint("claims/claims-api.v1.yaml", "GET", "/claims/{claimNumber}")
    assert "Required scopes: claims.read" in c.body
    assert "DEPRECATED, sunset 2026-12-31, replaced by claims-api v2" in c.body
    assert "NO (Norway)" in c.body  # spelled out: 'no' is an English stop word


def test_endpoint_chunk_resolves_refs_into_fields() -> None:
    c = _endpoint("claims/claims-search-api.v1.yaml", "GET", "/claims")
    assert "items[].claimNumber (string) required" in c.body
    assert "items[].reserve.currency" in c.body


def test_undocumented_spec_still_produces_searchable_chunks() -> None:
    c = _endpoint("documents/dk-document-archive-api.v1.yaml", "GET", "/doc")
    assert "cpr" in c.body and "polno" in c.body and "DK (Denmark)" in c.body


def test_chunk_hash_changes_only_with_content() -> None:
    a = ChunkDraft(key="k", kind="endpoint", title="t", body="b")
    assert a.content_hash == ChunkDraft(key="other", kind="field", title="t", body="b").content_hash
    assert a.content_hash != ChunkDraft(key="k", kind="endpoint", title="t", body="b2").content_hash


def test_render_schema_survives_recursive_refs() -> None:
    doc = {
        "components": {
            "schemas": {"Node": {"type": "object", "properties": {"child": {"$ref": "#/components/schemas/Node"}}}}
        }
    }
    lines = render_schema(doc, {"$ref": "#/components/schemas/Node"})
    assert lines and len(lines) < 20


def test_dereference_inlines_and_stops_cycles() -> None:
    doc = {
        "components": {
            "schemas": {
                "A": {"type": "object", "properties": {"b": {"$ref": "#/components/schemas/B"}}},
                "B": {"type": "object", "properties": {"a": {"$ref": "#/components/schemas/A"}}},
            }
        }
    }
    out = dereference(doc, {"$ref": "#/components/schemas/A"})
    assert out["properties"]["b"]["type"] == "object"
    assert "$comment" in out["properties"]["b"]["properties"]["a"]


# --------------------------------------------------------------------------- diff


def test_diff_policy_v1_to_v2_reports_breaking_changes() -> None:
    d = diff_specs(_doc("policy/policy-api.v1.yaml"), _doc("policy/policy-api.v2.yaml"))
    assert d.is_breaking
    assert "GET /policies/{policyId}/coverages" in d.endpoints_added
    cancel = next(c for c in d.endpoints_changed if c.old_path == "/policies/{policyNumber}/cancel")
    assert cancel.new_path == "/policies/{policyId}/cancellations"  # matched by operationId
    assert any("Idempotency-Key" in b for b in d.breaking_changes)
    assert any("'premium' removed" in b for b in d.breaking_changes)


def test_diff_of_identical_specs_is_empty() -> None:
    doc = _doc("quotes/quote-api.v2.yaml")
    d = diff_specs(doc, doc)
    assert not d.is_breaking
    assert not (d.endpoints_added or d.endpoints_removed or d.endpoints_changed or d.property_changes)


# --------------------------------------------------------------------------- filters / embeddings


def test_filter_sql_binds_every_value() -> None:
    evil = SearchFilters.model_construct(domain="claims'; DROP TABLE chunk; --", country="NO", deprecated=False)
    sql, params = _filter_sql(evil)
    assert "DROP" not in sql
    assert params["f_domain"].startswith("claims'")
    assert sql.count(":f_") == 3


def test_search_filters_reject_unknown_values() -> None:
    with pytest.raises(ValueError):
        SearchFilters(country="US")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        SearchFilters(domain="marketing")  # type: ignore[arg-type]


def test_hashing_embedder_is_deterministic_and_normalised() -> None:
    e = HashingEmbeddings(384)
    v1, v2 = e.embed_query("open claims"), e.embed_query("open claims")
    assert v1 == v2 and len(v1) == 384
    assert math.isclose(sum(x * x for x in v1), 1.0, rel_tol=1e-6)


def test_missing_model_gives_actionable_error(tmp_path: Path) -> None:
    with pytest.raises(EmbeddingError, match="make model"):
        create_provider(Settings(embedding_provider="onnx-local", embedding_model_dir=tmp_path))


def test_azure_provider_requires_credentials() -> None:
    with pytest.raises(EmbeddingError, match="AZURE_OPENAI"):
        create_provider(Settings(embedding_provider="azure-openai"))


@pytest.fixture(scope="module")
def onnx() -> Any:
    try:
        return create_provider(Settings(embedding_provider="onnx-local"))
    except EmbeddingError:
        pytest.skip("local ONNX model not installed (make model)")


def test_onnx_embeddings_are_semantic(onnx: Any) -> None:
    q = onnx.embed_query("Which API lists open claims in Norway?")
    docs = onnx.embed_documents(["Search claims by status and market", "Issue a green card certificate"])
    sims = [sum(a * b for a, b in zip(q, d, strict=True)) for d in docs]
    assert len(q) == 384
    assert sims[0] > sims[1]


def test_partial_x_nordlys_block_is_resolved_field_by_field() -> None:
    from nordlys_discovery.catalog.metadata import extract_metadata

    doc = {
        "openapi": "3.1.0",
        "info": {"title": "Vet Clinic Directory API", "version": "1.0.0", "x-nordlys": {"countries": ["SE", "NO"]}},
        "paths": {},
    }
    m = extract_metadata(doc, Path("/reg/apis/unknown/vet-clinic-directory-api.v1.yaml"))
    assert m.api_id == "vet-clinic-directory-api" and m.countries == ("SE", "NO")
    assert m.provenance["countries"] == "x-nordlys" and m.provenance["domain"] == "missing"
