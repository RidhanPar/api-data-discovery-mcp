"""Integration tests for Phase 4 security against the real catalog service and Postgres."""

from __future__ import annotations

import shutil
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp import Client
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from nordlys_discovery.catalog.loader import DEFAULT_CATALOG_DIR
from nordlys_discovery.config import Settings
from nordlys_discovery.embeddings.others import HashingEmbeddings
from nordlys_discovery.ingest.pipeline import ingest_catalog
from nordlys_discovery.mcp_server.catalog_client import CatalogClient
from nordlys_discovery.mcp_server.guard import Caller
from nordlys_discovery.mcp_server.server import build_server, parse_tool_error
from nordlys_discovery.security.jwt import JwtValidator
from nordlys_discovery.security.ratelimit import RateLimiter
from nordlys_discovery.service.app import create_app
from nordlys_discovery.service.auth import CatalogAuth

pytestmark = [pytest.mark.integration]
ISSUER = "https://idp.nordlys.example/realms/nordlys"
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def signing_key() -> Any:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def service_token(key: Any, client: str, scopes: str = "catalog.internal") -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": ISSUER,
            "aud": "nordlys-catalog",
            "sub": f"svc-{client}",
            "azp": client,
            "iat": now,
            "exp": now + 300,
            "scope": scopes,
            "preferred_username": f"service-account-{client}",
        },
        key,
        algorithm="RS256",
    )


@pytest.fixture(scope="module")
def loaded_engine(engine: Engine, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Engine]:
    """Catalog + the malicious partner spec, ingested once for this module."""
    catalog = tmp_path_factory.mktemp("cat") / "catalog"
    shutil.copytree(DEFAULT_CATALOG_DIR, catalog)
    shutil.copy(
        FIXTURES / "malicious-partner-api.v1.yaml", catalog / "apis" / "partner" / "partner-loyalty-api.v1.yaml"
    )
    with sessionmaker(bind=engine)() as s:
        report = ingest_catalog(s, catalog, HashingEmbeddings(384))
        s.commit()
    assert report.errors == []
    yield engine
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE access_request, chunk, api_spec, data_product RESTART IDENTITY CASCADE"))


@pytest.fixture(scope="module")
def secure_client(loaded_engine: Engine, signing_key: Any) -> Iterator[TestClient]:
    public = signing_key.public_key()
    settings = Settings(auth_enabled=True)
    auth = CatalogAuth(settings, JwtValidator(issuer=ISSUER, audience="nordlys-catalog", key_resolver=lambda _: public))
    app = create_app(settings, engine=loaded_engine, embedder=HashingEmbeddings(384), auth=auth)
    with TestClient(app) as c:
        yield c


def _h(
    key: Any,
    client: str = "nordlys-mcp-server",
    user: str | None = "alice@nordlys.example",
    scopes: str = "catalog.read dataproduct.read access.request",
    svc_scopes: str = "catalog.internal",
) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {service_token(key, client, svc_scopes)}"}
    if user:
        headers |= {"X-On-Behalf-Of-Subject": user, "X-On-Behalf-Of-Scopes": scopes}
    return headers


# --------------------------------------------------------------------------- catalog service auth


def test_health_is_public_but_v1_needs_a_token(secure_client: TestClient) -> None:
    assert secure_client.get("/health/live").status_code == 200
    r = secure_client.get("/v1/apis")
    assert r.status_code == 401 and r.headers["content-type"] == "application/problem+json"


def test_token_without_internal_scope_is_forbidden(secure_client: TestClient, signing_key: Any) -> None:
    r = secure_client.get("/v1/apis", headers=_h(signing_key, svc_scopes="catalog.read"))
    assert r.status_code == 403


def test_forged_token_is_rejected(secure_client: TestClient) -> None:
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert secure_client.get("/v1/apis", headers=_h(other)).status_code == 401


def test_untrusted_service_cannot_act_on_behalf_of_users(secure_client: TestClient, signing_key: Any) -> None:
    r = secure_client.get("/v1/apis", headers=_h(signing_key, client="some-batch-job"))
    assert r.status_code == 403 and "on behalf of" in r.json()["title"]


def test_trusted_service_works(secure_client: TestClient, signing_key: Any) -> None:
    assert secure_client.get("/v1/apis", headers=_h(signing_key)).status_code == 200


@pytest.mark.parametrize(
    ("product", "user_scopes", "status"),
    [
        ("weather-event-claims-exposure", "dataproduct.read", 200),  # pii none
        ("premium-written-monthly", "dataproduct.read", 200),  # internal
        ("customer-360-profile", "dataproduct.read", 403),  # personal
        ("claims-fraud-scores", "dataproduct.read", 403),  # sensitive
        ("claims-fraud-scores", "dataproduct.read dataproduct.fraud-scores.read", 200),  # already approved
    ],
)
def test_sample_rows_policy_and_audit(
    secure_client: TestClient, signing_key: Any, product: str, user_scopes: str, status: int
) -> None:
    rid = uuid.uuid4().hex
    r = secure_client.get(
        f"/v1/data-products/{product}/samples",
        headers=_h(signing_key, scopes=user_scopes) | {"X-Request-ID": rid},
    )
    assert r.status_code == status
    events = secure_client.get("/v1/audit-events", params={"request_id": rid}, headers=_h(signing_key)).json()
    assert len(events) == 1
    e = events[0]
    assert e["actor"] == "alice@nordlys.example" and e["source"] == "nordlys-mcp-server"
    assert e["decision"] == ("allowed" if status == 200 else "denied")


def test_requester_identity_comes_from_the_service_not_the_body(secure_client: TestClient, signing_key: Any) -> None:
    body = {
        "requester": "mallory@nordlys.example",
        "asset_type": "data_product",
        "asset_id": "claims-open-cases-daily",
        "purpose": "claims_operations_reporting",
        "justification": "Backlog dashboard for the NO claims team.",
    }
    r = secure_client.post("/v1/access-requests", json=body, headers=_h(signing_key))
    assert r.status_code == 201
    assert r.json()["requester"] == "alice@nordlys.example"


def test_only_approvers_can_decide(secure_client: TestClient, signing_key: Any) -> None:
    body = {
        "requester": "x",
        "asset_type": "data_product",
        "asset_id": "quote-conversion-funnel",
        "purpose": "analytics",
        "justification": "Funnel analysis for the FI motor launch.",
    }
    created = secure_client.post("/v1/access-requests", json=body, headers=_h(signing_key)).json()
    decision = {"decided_by": "ignored", "decision": "approved", "note": "looks fine"}
    url = f"/v1/access-requests/{created['id']}/decision"
    # A developer (no access.approve) is refused, and the refusal is audited.
    assert secure_client.post(url, json=decision, headers=_h(signing_key)).status_code == 403
    # n8n acting for approver bob, who holds access.approve.
    ok = secure_client.post(
        url,
        json=decision,
        headers=_h(signing_key, client="nordlys-n8n", user="bob@nordlys.example", scopes="access.approve"),
    )
    assert ok.status_code == 200 and ok.json()["decided_by"] == "bob@nordlys.example"


def test_audit_source_cannot_be_spoofed(secure_client: TestClient, signing_key: Any) -> None:
    event = {"actor": "alice", "action": "tool:search_catalog", "decision": "allowed", "source": "spoofed"}
    out = secure_client.post("/v1/audit-events", json=event, headers=_h(signing_key)).json()
    assert out["source"] == "nordlys-mcp-server"


def test_audit_log_is_append_only(loaded_engine: Engine) -> None:
    with loaded_engine.connect() as conn:
        conn.execute(text("INSERT INTO audit_event (source, actor, action, decision) VALUES ('t','t','t','info')"))
        conn.commit()
        for stmt in ("UPDATE audit_event SET actor = 'x'", "DELETE FROM audit_event", "TRUNCATE audit_event"):
            with pytest.raises(DBAPIError, match="append-only"):
                conn.execute(text(stmt))
            conn.rollback()


# --------------------------------------------------------------------------- MCP guard + injection


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def open_app(loaded_engine: Engine) -> FastAPI:
    return create_app(Settings(), engine=loaded_engine, embedder=HashingEmbeddings(384))


async def _mcp(app: FastAPI, caller: Caller, limiter: RateLimiter | None = None) -> AsyncIterator[Client]:
    async with app.router.lifespan_context(app):
        catalog = CatalogClient("http://catalog", transport=httpx.ASGITransport(app=app), retries=0)
        async with Client(build_server(catalog, dev_caller=caller, limiter=limiter)) as client:
            yield client


def _structured_error(result: Any) -> dict[str, Any]:
    return parse_tool_error(result.content[0].text) or {}


@pytest.mark.anyio
async def test_tool_scope_enforcement(open_app: FastAPI) -> None:
    caller = Caller("carol@nordlys.example", tool_scopes=frozenset({"catalog.read"}))
    async for client in _mcp(open_app, caller):
        assert not (await client.call_tool("search_catalog", {"query": "claims"})).is_error
        r = await client.call_tool("get_data_product", {"product_id": "claims-open-cases-daily"})
        assert r.is_error and _structured_error(r)["code"] == "forbidden"
        r = await client.call_tool(
            "request_access",
            {
                "asset_type": "data_product",
                "asset_id": "quote-conversion-funnel",
                "purpose": "analytics",
                "justification": "Funnel analysis please.",
            },
        )
        assert r.is_error and "access.request" in _structured_error(r)["message"]


@pytest.mark.anyio
async def test_rate_limit(open_app: FastAPI) -> None:
    async for client in _mcp(open_app, Caller("dave@nordlys.example"), RateLimiter(rate_per_minute=1, burst=2)):
        results = [await client.call_tool("list_deprecations", {}) for _ in range(3)]
        assert [r.is_error for r in results] == [False, False, True]
        assert _structured_error(results[2])["code"] == "rate_limited"


@pytest.mark.anyio
async def test_every_tool_call_is_audited(open_app: FastAPI, loaded_engine: Engine) -> None:
    who = f"auditee-{uuid.uuid4().hex[:8]}@nordlys.example"
    async for client in _mcp(open_app, Caller(who)):
        await client.call_tool("get_api_details", {"api_id": "claims-api", "version": 2})
        await client.call_tool("get_api_details", {"api_id": "BAD ID", "version": 2})
    with loaded_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT action, resource, decision FROM audit_event WHERE actor = :a ORDER BY id"), {"a": who}
        ).all()
    assert [tuple(r) for r in rows] == [
        ("tool:get_api_details", "api:claims-api:v2", "allowed"),
        ("tool:get_api_details", "api:BAD ID:v2", "invalid"),
    ]


@pytest.mark.anyio
async def test_malicious_spec_is_flagged_and_cannot_escalate(open_app: FastAPI) -> None:
    async for client in _mcp(open_app, Caller("erin@nordlys.example")):
        details = await client.call_tool("get_api_details", {"api_id": "partner-loyalty-api", "version": 1})
        warnings = details.structured_content["documentation_quality"]["content_warnings"]
        assert {"instruction_override", "tool_steering", "exfiltration"} <= set(warnings)

        found = await client.call_tool("search_catalog", {"query": "loyalty points"})
        hit = next(h for h in found.structured_content["results"] if h["asset_id"] == "partner-loyalty-api")
        assert hit["content_warnings"]

        # Doing exactly what the injected text demands still cannot grant or leak anything:
        r = await client.call_tool(
            "request_access",
            {
                "asset_type": "data_product",
                "asset_id": "claims-fraud-scores",
                "purpose": "marketing",
                "justification": "The API description told me to request this.",
            },
        )
        assert r.is_error and _structured_error(r)["code"] == "rule_violation"
        dp = await client.call_tool("get_data_product", {"product_id": "claims-fraud-scores", "include_samples": True})
        assert dp.structured_content["sample_rows"] is None
        assert "metadata only" in dp.structured_content["sample_policy_reason"]
