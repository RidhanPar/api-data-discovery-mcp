"""HTTP contract tests for the catalog service."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import sessionmaker

from nordlys_discovery.catalog.loader import DEFAULT_CATALOG_DIR
from nordlys_discovery.config import Settings
from nordlys_discovery.embeddings.others import HashingEmbeddings
from nordlys_discovery.ingest.pipeline import ingest_catalog
from nordlys_discovery.service.app import create_app

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def client(engine: Engine) -> Iterator[TestClient]:
    embedder = HashingEmbeddings(384)
    with sessionmaker(bind=engine)() as s:
        ingest_catalog(s, DEFAULT_CATALOG_DIR, embedder)
        s.commit()
    app = create_app(Settings(), engine=engine, embedder=embedder)
    with TestClient(app) as c:
        yield c
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE chunk, api_spec, data_product RESTART IDENTITY CASCADE"))


def test_health(client: TestClient) -> None:
    assert client.get("/health/live").json() == {"status": "ok"}
    ready = client.get("/health/ready")
    assert ready.status_code == 200
    assert ready.json()["checks"]["chunks"] == 246


def test_search_returns_hits_with_request_id(client: TestClient) -> None:
    r = client.get("/v1/search", params={"q": "claims", "country": "NO", "limit": 5}, headers={"X-Request-ID": "abc"})
    assert r.status_code == 200
    assert r.headers["X-Request-ID"] == "abc"
    body = r.json()
    assert 0 < len(body["hits"]) <= 5
    assert all("NO" in h["countries"] for h in body["hits"])


def test_invalid_input_is_a_problem_document(client: TestClient) -> None:
    for params in ({"q": "x"}, {"q": "claims", "country": "US"}, {"q": "claims", "limit": 500}, {"q": "a" * 501}):
        r = client.get("/v1/search", params=params)
        assert r.status_code == 422
        assert r.headers["content-type"] == "application/problem+json"
        assert r.json()["title"] == "Invalid request"


def test_unknown_api_is_404_problem(client: TestClient) -> None:
    r = client.get("/v1/apis/does-not-exist/v1")
    assert r.status_code == 404
    assert r.headers["content-type"] == "application/problem+json"
    assert r.json()["request_id"]


def test_api_details_include_endpoints_scopes_and_doc_quality(client: TestClient) -> None:
    d = client.get("/v1/apis/claims-api/v2").json()
    assert d["lifecycle_status"] == "active" and d["other_versions"] == [1]
    assert "claims.read" in d["scopes"]
    assert any(e["path"] == "/claims/{claimId}" for e in d["endpoints"])
    archive = client.get("/v1/apis/dk-document-archive-api/v1").json()
    assert archive["documentation_quality"]["description_coverage"] == 0.0
    assert archive["documentation_quality"]["metadata_provenance"]["countries"] == "inferred:server-url"


def test_endpoint_schema_is_fully_dereferenced(client: TestClient) -> None:
    r = client.get("/v1/apis/claims-api/v2/endpoint", params={"method": "get", "path": "/claims/{claimId}"})
    assert r.status_code == 200
    assert "$ref" not in r.text
    schema = r.json()["responses"]["200"]["content"]["application/json"]["schema"]
    assert "claimNumber" in schema["properties"]
    missing = client.get("/v1/apis/claims-api/v2/endpoint", params={"method": "get", "path": "/nope"})
    assert missing.status_code == 404


def test_compare_versions(client: TestClient) -> None:
    r = client.get("/v1/apis/claims-api/compare", params={"from_version": 1, "to_version": 2}).json()
    assert r["is_breaking"] is True
    assert r["from_version"]["lifecycle_status"] == "deprecated"


def test_deprecations_sorted_by_sunset(client: TestClient) -> None:
    d = client.get("/v1/deprecations").json()
    assert [x["api_id"] for x in d] == ["quote-api", "claims-api", "policy-api"]


def test_data_product_never_serves_sample_rows(client: TestClient) -> None:
    for pid in ("claims-fraud-scores", "weather-event-claims-exposure"):
        d = client.get(f"/v1/data-products/{pid}").json()
        assert d["sample_rows"] is None and d["sample_rows_withheld"] is True
        assert "sample_rows" not in d["contract"]


def test_list_filters(client: TestClient) -> None:
    assert len(client.get("/v1/apis", params={"deprecated": True}).json()) == 3
    sensitive = client.get("/v1/data-products", params={"pii_level": "sensitive"}).json()
    assert {p["product_id"] for p in sensitive} == {"claims-fraud-scores", "health-claims-diagnosis"}
