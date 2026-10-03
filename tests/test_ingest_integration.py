"""Integration tests: ingestion + search against real Postgres/pgvector."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nordlys_discovery.catalog.loader import DEFAULT_CATALOG_DIR
from nordlys_discovery.catalog.models import PiiLevel
from nordlys_discovery.db.models import ApiSpec, Chunk, DataProductRow
from nordlys_discovery.embeddings.others import HashingEmbeddings
from nordlys_discovery.ingest.pipeline import ingest_catalog
from nordlys_discovery.search.hybrid import lexical_search, search
from nordlys_discovery.search.models import SearchFilters, SearchQuery

pytestmark = pytest.mark.integration


def _count(session: Session, model: type) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def test_first_ingest_loads_everything(session: Session, hashing_embedder: HashingEmbeddings) -> None:
    r = ingest_catalog(session, DEFAULT_CATALOG_DIR, hashing_embedder)
    assert r.errors == []
    assert r.apis["added"] == 25 and r.data_products["added"] == 15
    assert r.chunks["added"] == r.chunks["embedded"] == _count(session, Chunk) == 246
    assert session.scalar(select(func.count()).select_from(Chunk).where(Chunk.embedding.is_(None))) == 0


def test_reingest_is_a_no_op(session: Session, hashing_embedder: HashingEmbeddings) -> None:
    ingest_catalog(session, DEFAULT_CATALOG_DIR, hashing_embedder)
    r = ingest_catalog(session, DEFAULT_CATALOG_DIR, hashing_embedder)
    assert r.apis == {"added": 0, "updated": 0, "unchanged": 25, "deleted": 0}
    assert r.data_products["unchanged"] == 15
    assert r.chunks["embedded"] == 0 and r.chunks["added"] == 0 and r.chunks["deleted"] == 0


def test_changed_spec_reembeds_only_changed_chunks(
    session: Session, hashing_embedder: HashingEmbeddings, catalog_copy: Path
) -> None:
    ingest_catalog(session, catalog_copy, hashing_embedder)
    spec = catalog_copy / "apis" / "claims" / "fnol-api.v1.yaml"
    spec.write_text(
        spec.read_text(encoding="utf-8").replace(
            "summary: List valid loss cause codes per product line",
            "summary: List valid loss cause codes per product line and language",
        ),
        encoding="utf-8",
    )
    r = ingest_catalog(session, catalog_copy, hashing_embedder)
    assert r.apis["updated"] == 1 and r.apis["unchanged"] == 24
    # The changed endpoint + the API overview (which lists summaries) change; nothing else.
    assert r.chunks["updated"] == 2
    assert r.chunks["embedded"] == 2


def test_removed_file_is_pruned_with_its_chunks(
    session: Session, hashing_embedder: HashingEmbeddings, catalog_copy: Path
) -> None:
    ingest_catalog(session, catalog_copy, hashing_embedder)
    (catalog_copy / "data-products" / "document-processing-metrics.yaml").unlink()
    r = ingest_catalog(session, catalog_copy, hashing_embedder)
    assert r.data_products["deleted"] == 1
    assert r.chunks["deleted"] == 7  # overview + 6 fields
    assert session.scalar(select(DataProductRow).filter_by(product_id="document-processing-metrics")) is None
    assert (
        session.scalar(select(func.count()).select_from(Chunk).filter_by(asset_id="document-processing-metrics")) == 0
    )


def test_invalid_file_is_reported_and_existing_data_kept(
    session: Session, hashing_embedder: HashingEmbeddings, catalog_copy: Path
) -> None:
    ingest_catalog(session, catalog_copy, hashing_embedder)
    broken = catalog_copy / "apis" / "fraud" / "fraud-signals-api.v1.yaml"
    broken.write_text("openapi: 3.1.0\ninfo: {}\npaths: {}\n", encoding="utf-8")
    r = ingest_catalog(session, catalog_copy, hashing_embedder)
    assert len(r.errors) == 1 and "fraud-signals-api" in r.errors[0]
    assert r.apis["deleted"] == 0
    assert session.scalar(select(ApiSpec).filter_by(api_id="fraud-signals-api")) is not None


def test_changing_embedding_model_reembeds_everything(session: Session, catalog_copy: Path) -> None:
    ingest_catalog(session, catalog_copy, HashingEmbeddings(384))

    class OtherModel(HashingEmbeddings):
        @property
        def model_id(self) -> str:
            return "hashing:v2"

    r = ingest_catalog(session, catalog_copy, OtherModel(384))
    assert r.chunks["embedded"] == 246 and r.chunks["updated"] == 0


# --------------------------------------------------------------------------- search


@pytest.fixture
def loaded(session: Session, hashing_embedder: HashingEmbeddings) -> Session:
    ingest_catalog(session, DEFAULT_CATALOG_DIR, hashing_embedder)
    return session


def test_bm25_finds_exact_identifiers(loaded: Session) -> None:
    hits = lexical_search(loaded, "Autogiro AvtaleGiro Betalingsservice mandate", SearchFilters(), _params())
    top = loaded.get(Chunk, hits[0][0])
    assert top is not None and top.asset_id == "payment-methods-api"


def test_bm25_ignores_stop_word_only_queries(loaded: Session) -> None:
    assert lexical_search(loaded, "the and of", SearchFilters(), _params()) == []


@pytest.mark.parametrize(
    ("filters", "check"),
    [
        (SearchFilters(domain="fraud"), lambda h: h.domain == "fraud"),
        (SearchFilters(country="LT"), lambda h: "LT" in h.countries),
        (SearchFilters(version=2), lambda h: h.major_version == 2),
        (SearchFilters(deprecated=True), lambda h: h.deprecated),
        (SearchFilters(asset_type="data_product"), lambda h: h.asset_type == "data_product"),
        (SearchFilters(pii_levels=[PiiLevel.SENSITIVE]), lambda h: h.pii_level == "sensitive"),
    ],
)
def test_filters_apply_in_every_mode(loaded: Session, hashing_embedder: HashingEmbeddings, filters, check) -> None:  # type: ignore[no-untyped-def]
    for mode in ("lexical", "vector", "hybrid"):
        r = search(
            loaded, hashing_embedder, SearchQuery(query="claims customer data", filters=filters, mode=mode, limit=20)
        )
        assert r.hits, f"{mode} returned nothing for {filters}"
        assert all(check(h) for h in r.hits), f"{mode} leaked a hit outside {filters}"


def test_deprecated_filter_returns_exactly_the_three_deprecated_apis(
    loaded: Session, hashing_embedder: HashingEmbeddings
) -> None:
    r = search(loaded, hashing_embedder, SearchQuery(query="api", filters=SearchFilters(deprecated=True), limit=50))
    assert {(h.asset_id, h.major_version) for h in r.hits} <= {("quote-api", 1), ("claims-api", 1), ("policy-api", 1)}
    assert all(h.sunset is not None and h.replacement for h in r.hits)


def test_hits_are_collapsed_per_asset(loaded: Session, hashing_embedder: HashingEmbeddings) -> None:
    r = search(loaded, hashing_embedder, SearchQuery(query="claims", limit=50))
    keys = [(h.asset_type, h.asset_id, h.major_version) for h in r.hits]
    assert len(keys) == len(set(keys))


def _params():  # type: ignore[no-untyped-def]
    from nordlys_discovery.search.hybrid import SearchParams

    return SearchParams()
