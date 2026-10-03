"""Agent service: the generative steps of the platform.

    uv run uvicorn nordlys_discovery.agent.app:app --port 8002

Endpoints
  POST /v1/classify-registration   Workflow B: classify a new API registration
  (Phase 5 adds /v1/ask: the discovery agent that answers developer questions via MCP)

When no LLM is configured the endpoints still answer, with `available: false`, so callers
can fall back to human review instead of failing.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..config import Settings, get_settings
from ..llm import LLMError, LLMProvider, LLMUnavailable, create_llm
from ..security.jwt import JwtValidator
from ..service.auth import CallContext, CatalogAuth
from .classify import ClassificationResult, classify

log = logging.getLogger(__name__)


class ClassifyIn(BaseModel):
    check: dict[str, Any] = Field(description="RegistrationCheck from the catalog's /v1/registrations/validate")


def _ctx(request: Request) -> CallContext:
    auth: CatalogAuth = request.app.state.auth
    return auth(request)


# Module level: FastAPI cannot resolve an Annotated alias defined inside a function
# when annotations are postponed (from __future__ import annotations).
Ctx = Annotated[CallContext, Depends(_ctx)]


def create_app(settings: Settings | None = None, *, llm: LLMProvider | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
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

    @app.middleware("http")
    async def request_id(request: Request, call_next: Any) -> Any:
        request.state.request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

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

    return app


app = create_app()
