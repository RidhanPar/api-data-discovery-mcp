"""Agent service: the generative steps of the platform.

    uv run uvicorn nordlys_discovery.agent.app:app --port 8002

Endpoints
  POST /v1/classify-registration   Workflow B: classify a new API registration
  POST /v1/ask                     the discovery agent: answers questions using MCP tools only

When no LLM is configured the endpoints still answer, with `available: false`, so callers
can fall back to human review instead of failing.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

import httpx2
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from pydantic import BaseModel, Field

from ..config import Settings, get_settings
from ..llm import LLMError, LLMProvider, LLMUnavailable, create_llm
from ..mcp_server.catalog_client import ClientCredentialsTokens
from ..observability import configure_logging, request_id_var
from ..security.jwt import JwtValidator
from ..security.ratelimit import RateLimiter
from ..service.auth import CallContext, CatalogAuth
from .classify import ClassificationResult, classify
from .discover import AgentAnswer, McpToolSource, ask

log = logging.getLogger(__name__)


class ClassifyIn(BaseModel):
    check: dict[str, Any] = Field(description="RegistrationCheck from the catalog's /v1/registrations/validate")


class AskIn(BaseModel):
    question: str = Field(min_length=3, max_length=2000)


def _ctx(request: Request) -> CallContext:
    auth: CatalogAuth = request.app.state.auth
    return auth(request)


# Module level: FastAPI cannot resolve an Annotated alias defined inside a function
# when annotations are postponed (from __future__ import annotations).
Ctx = Annotated[CallContext, Depends(_ctx)]


def create_app(
    settings: Settings | None = None, *, llm: LLMProvider | None = None, configure_logs: bool = False
) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if configure_logs:
            configure_logging("agent", settings.log_level, json_logs=settings.log_json)
        app.state.llm = llm
        app.state.llm_error = None
        if llm is None:
            try:
                app.state.llm = create_llm(settings)
            except LLMUnavailable as exc:
                app.state.llm_error = str(exc)
                log.warning("agent running without an LLM: %s", exc)
        yield

    app = FastAPI(title="Nordlys Agent Service", version="0.5.0", lifespan=lifespan)
    validator = (
        JwtValidator(issuer=settings.oidc_issuer, audience=settings.agent_audience, jwks_url=settings.oidc_jwks_url)
        if settings.auth_enabled
        else None
    )
    app.state.auth = CatalogAuth(settings, validator)
    tokens = None
    if settings.auth_enabled and settings.agent_client_secret is not None:
        token_url = (settings.oidc_jwks_url or settings.oidc_issuer + "/protocol/openid-connect/certs").replace(
            "/certs", "/token"
        )
        tokens = ClientCredentialsTokens(
            token_url, settings.agent_client_id, settings.agent_client_secret.get_secret_value()
        )
    # The agent is one MCP caller for many users, so users are rate-limited here.
    limiter = RateLimiter(settings.rate_limit_per_minute / 6, max(2, settings.rate_limit_burst // 4))

    @app.middleware("http")
    async def request_id(request: Request, call_next: Any) -> Any:
        request.state.request_id = (request.headers.get("X-Request-ID") or uuid.uuid4().hex)[:80]
        token = request_id_var.set(request.state.request_id)
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request.state.request_id
            return response
        finally:
            request_id_var.reset(token)

    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready(request: Request) -> JSONResponse:
        llm_ = request.app.state.llm
        body = {"status": "ok", "llm": llm_.model_id if llm_ else f"unavailable: {request.app.state.llm_error}"}
        return JSONResponse(body)  # ready either way: callers handle available=false

    @app.post("/v1/classify-registration", response_model=ClassificationResult)
    async def classify_registration(request: Request, ctx: Ctx, body: ClassifyIn) -> ClassificationResult:
        llm_: LLMProvider | None = request.app.state.llm
        if llm_ is None:
            return ClassificationResult(available=False, reason=request.app.state.llm_error)
        try:
            return await classify(llm_, body.check)
        except LLMError as exc:
            log.error("classification failed: %s", exc)
            return ClassificationResult(available=False, model=llm_.model_id, reason=f"LLM error: {exc}")

    @app.post("/v1/ask", response_model=AgentAnswer)
    async def ask_question(request: Request, ctx: Ctx, body: AskIn) -> AgentAnswer:
        llm_: LLMProvider | None = request.app.state.llm
        if llm_ is None:
            raise HTTPException(503, f"no LLM configured: {request.app.state.llm_error}")
        allowed, retry_after = limiter.allow(ctx.user)
        if not allowed:
            raise HTTPException(429, "rate limit exceeded", headers={"Retry-After": str(int(retry_after) + 1)})
        headers = {"X-Request-ID": request.state.request_id}
        if settings.auth_enabled:
            if tokens is None:
                raise HTTPException(503, "NORDLYS_AGENT_CLIENT_SECRET is not configured")
            headers["Authorization"] = f"Bearer {await tokens()}"
        async with (
            httpx2.AsyncClient(headers=headers, timeout=30) as http,
            Client(streamable_http_client(settings.agent_mcp_url, http_client=http)) as client,
        ):
            answer = await ask(llm_, McpToolSource(client), body.question, max_steps=settings.agent_max_steps)
        log.info(
            "agent_answer",
            extra={
                "event": "agent_answer",
                "user": ctx.user,
                "model": answer.model,
                "stop": answer.stop,
                "refused": answer.refused,
                "tool_calls": len(answer.tool_calls),
                "ungrounded_citations": len(answer.ungrounded_citations),
                "input_tokens": answer.input_tokens,
                "output_tokens": answer.output_tokens,
                "latency_ms": answer.latency_ms,
            },
        )
        return answer

    return app


app = create_app(configure_logs=True)
