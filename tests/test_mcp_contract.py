"""MCP contract tests.

The real MCP server runs in-process and talks to the real catalog service (FastAPI app
on a test database) through an in-memory ASGI transport, so these tests cover the full
path a client sees: tool schemas, validation, structured output, structured errors,
resources and prompts.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from mcp import Client
from sqlalchemy import Engine, text
from sqlalchemy.orm import sessionmaker

from nordlys_discovery.catalog.loader import DEFAULT_CATALOG_DIR
from nordlys_discovery.config import Settings
from nordlys_discovery.embeddings.others import HashingEmbeddings
from nordlys_discovery.ingest.pipeline import ingest_catalog
from nordlys_discovery.mcp_server.catalog_client import CatalogClient
from nordlys_discovery.mcp_server.server import Caller, build_server, parse_tool_error
from nordlys_discovery.service.app import create_app

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

EXPECTED_TOOLS = {
    "search_catalog",
    "get_api_details",
    "get_endpoint_schema",
    "compare_api_versions",
    "get_data_product",
    "check_access",
    "request_access",
    "list_deprecations",
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="module")
def catalog_app(engine: Engine) -> Iterator[FastAPI]:
    with sessionmaker(bind=engine)() as s:
        ingest_catalog(s, DEFAULT_CATALOG_DIR, HashingEmbeddings(384))
        s.commit()
    yield create_app(Settings(), engine=engine, embedder=HashingEmbeddings(384))
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE access_request, chunk, api_spec, data_product RESTART IDENTITY CASCADE"))


class CountingTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner, self.calls = inner, 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return await self.inner.handle_async_request(request)


async def _client(app: FastAPI, caller: Caller) -> AsyncIterator[tuple[Client, CountingTransport]]:
    transport = CountingTransport(httpx.ASGITransport(app=app))
    async with app.router.lifespan_context(app):
        server = build_server(CatalogClient("http://catalog", transport=transport, retries=0), lambda: caller)
        async with Client(server) as client:
            yield client, transport


@pytest.fixture
async def mcp(catalog_app: FastAPI, engine: Engine) -> AsyncIterator[tuple[Client, CountingTransport]]:
    async for pair in _client(catalog_app, Caller("dev.user@nordlys.example")):
        yield pair
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE access_request"))


async def _ok(client: Client, tool: str, args: dict[str, Any]) -> Any:
    r = await client.call_tool(tool, args)
    assert not r.is_error, r.content
    return r.structured_content


async def _err(client: Client, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    r = await client.call_tool(tool, args)
    assert r.is_error
    text_ = r.content[0].text  # type: ignore[union-attr]
    return parse_tool_error(text_) or {"code": "unstructured", "message": text_}


# --------------------------------------------------------------------------- discovery surface


async def test_tools_are_listed_with_descriptions_and_schemas(mcp: tuple[Client, CountingTransport]) -> None:
    client, _ = mcp
    tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == EXPECTED_TOOLS
    for t in tools.values():
        assert t.description and len(t.description) > 60, f"{t.name} needs a useful description"
        assert t.output_schema is not None, f"{t.name} should return structured output"
        assert t.annotations is not None
    assert tools["request_access"].annotations.read_only_hint is False  # type: ignore[union-attr]
    assert all(tools[n].annotations.read_only_hint for n in EXPECTED_TOOLS - {"request_access"})  # type: ignore[union-attr]


async def test_input_schemas_carry_constraints(mcp: tuple[Client, CountingTransport]) -> None:
    client, _ = mcp
    tools = {t.name: t.input_schema for t in (await client.list_tools()).tools}
    schema_text = json.dumps(tools)
    assert "^[a-z][a-z0-9-]{1,119}$" in schema_text  # asset id pattern
    assert '"LT"' in json.dumps(tools["search_catalog"])  # country enum
    assert set(tools["request_access"]["required"]) >= {"asset_type", "asset_id", "purpose", "justification"}


async def test_invalid_arguments_never_reach_the_catalog(mcp: tuple[Client, CountingTransport]) -> None:
    client, transport = mcp
    before = transport.calls
    for tool, args in [
        ("get_api_details", {"api_id": "DROP TABLE", "version": 1}),
        ("get_api_details", {"api_id": "claims-api", "version": 0}),
        ("search_catalog", {"query": "x"}),
        ("search_catalog", {"query": "claims", "country": "US"}),
        ("search_catalog", {"query": "claims", "limit": 500}),
        ("get_endpoint_schema", {"api_id": "claims-api", "version": 2, "method": "TRACE", "path": "/claims"}),
        (
            "request_access",
            {"asset_type": "api", "asset_id": "claims-api", "version": 2, "purpose": "x", "justification": "short"},
        ),
    ]:
        r = await client.call_tool(tool, args)
        assert r.is_error, (tool, args)
    assert transport.calls == before


async def test_search_returns_citations(mcp: tuple[Client, CountingTransport]) -> None:
    client, _ = mcp
    out = await _ok(client, "search_catalog", {"query": "claims", "country": "NO", "asset_type": "api", "limit": 5})
    assert out["results"] and out["filters_applied"] == {"country": "NO", "asset_type": "api"}
    for r in out["results"]:
        assert r["citation"].startswith(f"{r['asset_id']} v{r['version']}")
        assert "NO" in r["countries"]


async def test_details_schema_and_compare(mcp: tuple[Client, CountingTransport]) -> None:
    client, _ = mcp
    d = await _ok(client, "get_api_details", {"api_id": "claims-api", "version": 2})
    assert d["lifecycle_status"] == "active"
    s = await _ok(
        client,
        "get_endpoint_schema",
        {"api_id": "claims-api", "version": 2, "method": "GET", "path": "/claims/{claimId}"},
    )
    assert "$ref" not in json.dumps(s)
    c = await _ok(client, "compare_api_versions", {"api_id": "policy-api", "from_version": 1, "to_version": 2})
    assert c["is_breaking"] and c["diff"]["breaking_changes"]


async def test_not_found_is_a_structured_error(mcp: tuple[Client, CountingTransport]) -> None:
    client, _ = mcp
    e = await _err(client, "get_api_details", {"api_id": "no-such-api", "version": 1})
    assert e["code"] == "not_found" and e["retryable"] is False


async def test_data_product_never_includes_samples(mcp: tuple[Client, CountingTransport]) -> None:
    client, _ = mcp
    for pid in ("claims-fraud-scores", "weather-event-claims-exposure"):
        d = await _ok(client, "get_data_product", {"product_id": pid})
        assert d["sample_rows"] is None and "sample_rows" not in d["contract"]


async def test_list_deprecations(mcp: tuple[Client, CountingTransport]) -> None:
    client, _ = mcp
    out = await _ok(client, "list_deprecations", {})
    assert [d["api_id"] for d in out["deprecations"]] == ["quote-api", "claims-api", "policy-api"]
    soon = await _ok(client, "list_deprecations", {"sunset_before": "2026-12-01"})
    assert [d["api_id"] for d in soon["deprecations"]] == ["quote-api"]


# --------------------------------------------------------------------------- access


async def test_request_access_creates_pending_and_is_idempotent(mcp: tuple[Client, CountingTransport]) -> None:
    client, _ = mcp
    args = {
        "asset_type": "api",
        "asset_id": "claims-search-api",
        "version": 1,
        "purpose": "claims_operations_reporting",
        "justification": "Backlog dashboard for the Norwegian claims team.",
    }
    first = await _ok(client, "request_access", args)
    assert first["outcome"] == "created"
    assert first["request"]["status"] == "pending_approval"
    assert "has not been granted" in first["message"]
    second = await _ok(client, "request_access", args)
    assert second["outcome"] == "already_pending" and second["request"]["id"] == first["request"]["id"]


async def test_request_access_refuses_prohibited_purpose(mcp: tuple[Client, CountingTransport]) -> None:
    client, _ = mcp
    e = await _err(
        client,
        "request_access",
        {
            "asset_type": "data_product",
            "asset_id": "claims-fraud-scores",
            "purpose": "marketing",
            "justification": "We would like to target customers with offers.",
        },
    )
    assert e["code"] == "rule_violation" and "not permitted" in e["message"]


async def test_caller_with_scope_needs_no_request(catalog_app: FastAPI) -> None:
    async for client, _ in _client(catalog_app, Caller("svc@nordlys.example", frozenset({"claims.search"}))):
        check = await _ok(client, "check_access", {"asset_type": "api", "asset_id": "claims-search-api", "version": 1})
        assert check["decision"]["has_access"] is True
        out = await _ok(
            client,
            "request_access",
            {
                "asset_type": "api",
                "asset_id": "claims-search-api",
                "version": 1,
                "purpose": "claims_operations_reporting",
                "justification": "Already have it, just checking.",
            },
        )
        assert out["outcome"] == "not_needed" and out["request"] is None


# --------------------------------------------------------------------------- resources and prompts


async def test_resources(mcp: tuple[Client, CountingTransport]) -> None:
    client, _ = mcp
    templates = {t.uri_template for t in (await client.list_resource_templates()).resource_templates}
    assert templates == {"nordlys://openapi/{api_id}/{version}", "nordlys://data-contracts/{product_id}"}
    index = json.loads((await client.read_resource("nordlys://catalog/index")).contents[0].text)  # type: ignore[union-attr]
    assert len(index["apis"]) == 25 and len(index["data_products"]) == 15
    spec = json.loads((await client.read_resource("nordlys://openapi/claims-api/2")).contents[0].text)  # type: ignore[union-attr]
    assert spec["openapi"].startswith("3.1")
    contract = json.loads((await client.read_resource("nordlys://data-contracts/claims-fraud-scores")).contents[0].text)  # type: ignore[union-attr]
    assert contract["pii_classification"] == "sensitive" and "sample_rows" not in contract


async def test_prompts(mcp: tuple[Client, CountingTransport]) -> None:
    client, _ = mcp
    names = {p.name for p in (await client.list_prompts()).prompts}
    assert names == {"integrate_with_api", "explain_data_product"}
    p = await client.get_prompt(
        "integrate_with_api", {"api_id": "claims-api", "version": "2", "use_case": "read a claim"}
    )
    body = p.messages[0].content.text  # type: ignore[union-attr]
    assert "get_api_details" in body and "check_access" in body


# --------------------------------------------------------------------------- resilience


async def test_catalog_outage_is_retryable_structured_error() -> None:
    class Down(httpx.AsyncBaseTransport):
        calls = 0

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            Down.calls += 1
            raise httpx.ConnectError("connection refused", request=request)

    server = build_server(CatalogClient("http://catalog", transport=Down(), retries=2), lambda: Caller("x"))
    async with Client(server) as client:
        e = await _err(client, "get_api_details", {"api_id": "claims-api", "version": 2})
    assert e["code"] == "catalog_unavailable" and e["retryable"] is True
    assert Down.calls == 3  # first try + 2 retries


async def test_upstream_contract_violation_is_reported_not_crashed() -> None:
    class Weird(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"unexpected": True})

    server = build_server(CatalogClient("http://catalog", transport=Weird()), lambda: Caller("x"))
    async with Client(server) as client:
        e = await _err(client, "get_api_details", {"api_id": "claims-api", "version": 2})
    assert e["code"] == "upstream_contract_error"
