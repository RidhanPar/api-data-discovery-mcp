"""Catalog service: REST API over the ingested catalog.

    uv run uvicorn nordlys_discovery.service.app:app --port 8000

This is the system of record the MCP server (Phase 3) calls. It is read-only:
ingestion runs as a separate job (`python -m nordlys_discovery.ingest`).
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from ..access.policy import AccessRequirements
from ..catalog.models import PiiLevel
from ..config import Settings, get_settings
from ..db.models import Chunk
from ..db.session import make_engine
from ..embeddings import EmbeddingError, EmbeddingProvider, create_provider
from ..search.hybrid import SearchParams, search
from ..search.models import AssetType, Country, Domain, SearchFilters, SearchMode, SearchQuery, SearchResponse
from . import access
from . import repository as repo
from . import schemas as S

log = logging.getLogger(__name__)
PROBLEM_JSON = "application/problem+json"


def db(request: Request) -> Iterator[Session]:
    """One session per request, from the pool created at startup."""
    session: Session = request.app.state.sessions()
    try:
        yield session
        session.commit()  # read-only in practice; ends the transaction cleanly
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


DB = Annotated[Session, Depends(db)]


def create_app(
    settings: Settings | None = None, *, engine: Engine | None = None, embedder: EmbeddingProvider | None = None
) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.engine = engine or make_engine(
            settings.database_url,
            pool_size=settings.db_pool_size,
            statement_timeout_ms=settings.db_statement_timeout_ms,
        )
        app.state.sessions = sessionmaker(bind=app.state.engine, expire_on_commit=False)
        try:
            app.state.embedder = embedder or create_provider(settings)
        except EmbeddingError as exc:
            # Start anyway so /health/ready can report the problem; vector search will 503.
            log.error("embedding provider unavailable: %s", exc)
            app.state.embedder = None
        yield
        if engine is None:
            app.state.engine.dispose()

    app = FastAPI(
        title="Nordlys Catalog Service",
        version="0.2.0",
        summary="Search and inspect Nordlys Insurance APIs and Data Products.",
        lifespan=lifespan,
    )
    params = SearchParams(
        candidates=settings.search_candidates, rrf_k=settings.rrf_k, bm25_k1=settings.bm25_k1, bm25_b=settings.bm25_b
    )

    # ------------------------------------------------------------------ plumbing

    @app.middleware("http")
    async def request_id(request: Request, call_next: Any) -> Any:
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        request.state.request_id = rid
        started = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        response.headers["Server-Timing"] = f"app;dur={(time.perf_counter() - started) * 1000:.1f}"
        return response

    def problem(request: Request, status: int, title: str, detail: str | None = None) -> JSONResponse:
        body = S.Problem(
            title=title,
            status=status,
            detail=detail,
            instance=str(request.url.path),
            request_id=getattr(request.state, "request_id", None),
        )
        return JSONResponse(body.model_dump(exclude_none=True), status_code=status, media_type=PROBLEM_JSON)

    @app.exception_handler(repo.NotFound)
    async def _not_found(request: Request, exc: repo.NotFound) -> JSONResponse:
        return problem(request, 404, "Not found", f"{exc.what} does not exist in the catalog")

    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        detail = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        return problem(request, 422, "Invalid request", detail)

    @app.exception_handler(HTTPException)
    async def _http(request: Request, exc: HTTPException) -> JSONResponse:
        return problem(request, exc.status_code, str(exc.detail))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error", extra={"request_id": getattr(request.state, "request_id", None)})
        return problem(request, 500, "Internal error", "Quote the request id when reporting this.")

    # ------------------------------------------------------------------ health

    @app.get("/health/live", tags=["health"])
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", tags=["health"])
    def ready(request: Request, session: DB) -> JSONResponse:
        checks: dict[str, Any] = {}
        try:
            session.execute(text("SELECT 1"))
            total = session.scalar(select(func.count()).select_from(Chunk)) or 0
            pending = session.scalar(select(func.count()).select_from(Chunk).where(Chunk.embedding.is_(None))) or 0
            checks |= {"database": "ok", "chunks": total, "chunks_missing_embeddings": pending}
        except Exception as exc:
            checks["database"] = f"error: {type(exc).__name__}"
        embedder = request.app.state.embedder
        checks["embedding_model"] = embedder.model_id if embedder else "unavailable"
        ok = checks.get("database") == "ok" and embedder is not None and checks.get("chunks", 0) > 0
        return JSONResponse({"status": "ok" if ok else "unavailable", "checks": checks}, status_code=200 if ok else 503)

    # ------------------------------------------------------------------ search

    @app.get("/v1/search", response_model=SearchResponse, tags=["search"])
    def search_catalog(
        request: Request,
        session: DB,
        q: Annotated[str, Query(min_length=2, max_length=500, description="Natural-language or keyword query")],
        mode: SearchMode = "hybrid",
        asset_type: AssetType | None = None,
        domain: Domain | None = None,
        country: Country | None = None,
        version: Annotated[int | None, Query(ge=1, le=99)] = None,
        deprecated: bool | None = None,
        pii_level: Annotated[list[PiiLevel] | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=50)] = 10,
    ) -> SearchResponse:
        embedder = request.app.state.embedder
        if embedder is None and mode != "lexical":
            raise HTTPException(503, "Embedding model unavailable; use mode=lexical")
        query = SearchQuery(
            query=q,
            mode=mode,
            limit=limit,
            filters=SearchFilters(
                asset_type=asset_type,
                domain=domain,
                country=country,
                version=version,
                deprecated=deprecated,
                pii_levels=pii_level,
            ),
        )
        return search(session, embedder, query, params)

    # ------------------------------------------------------------------ APIs

    @app.get("/v1/apis", response_model=list[S.ApiSummary], tags=["apis"])
    def list_apis(
        session: DB, domain: Domain | None = None, country: Country | None = None, deprecated: bool | None = None
    ) -> list[S.ApiSummary]:
        return repo.list_apis(session, domain=domain, country=country, deprecated=deprecated)

    @app.get("/v1/apis/{api_id}", response_model=list[S.ApiSummary], tags=["apis"])
    def api_versions(session: DB, api_id: str) -> list[S.ApiSummary]:
        return repo.api_versions(session, api_id)

    @app.get("/v1/apis/{api_id}/v{major}", response_model=S.ApiDetails, tags=["apis"])
    def api_details(session: DB, api_id: str, major: int) -> S.ApiDetails:
        return repo.api_details(session, api_id, major)

    @app.get("/v1/apis/{api_id}/v{major}/endpoint", response_model=S.EndpointSchema, tags=["apis"])
    def endpoint_schema(
        session: DB,
        api_id: str,
        major: int,
        method: Annotated[str, Query(pattern="^(?i)(get|post|put|patch|delete)$")],
        path: Annotated[str, Query(min_length=1, max_length=300, pattern="^/")],
    ) -> S.EndpointSchema:
        return repo.endpoint_schema(session, api_id, major, method, path)

    @app.get("/v1/apis/{api_id}/v{major}/spec", tags=["apis"])
    def raw_spec(session: DB, api_id: str, major: int) -> dict[str, Any]:
        return repo.raw_spec(session, api_id, major)

    @app.get("/v1/apis/{api_id}/compare", response_model=S.VersionComparison, tags=["apis"])
    def compare(
        session: DB, api_id: str, from_version: Annotated[int, Query(ge=1)], to_version: Annotated[int, Query(ge=1)]
    ) -> S.VersionComparison:
        return repo.compare_versions(session, api_id, from_version, to_version)

    @app.get("/v1/deprecations", response_model=list[S.Deprecation], tags=["apis"])
    def deprecations(session: DB, sunset_before: date | None = None) -> list[S.Deprecation]:
        return repo.deprecations(session, today=date.today(), sunset_before=sunset_before)

    # ------------------------------------------------------------------ data products

    @app.get("/v1/data-products", response_model=list[S.DataProductSummary], tags=["data-products"])
    def list_data_products(
        session: DB, domain: Domain | None = None, country: Country | None = None, pii_level: PiiLevel | None = None
    ) -> list[S.DataProductSummary]:
        return repo.list_data_products(
            session, domain=domain, country=country, pii_level=pii_level.value if pii_level else None
        )

    @app.get("/v1/data-products/{product_id}", response_model=S.DataProductDetails, tags=["data-products"])
    def get_data_product(session: DB, product_id: str) -> S.DataProductDetails:
        # Sample rows are never served here; Phase 4 adds a policy-checked path for them.
        return repo.data_product(session, product_id, include_samples=False)

    # ------------------------------------------------------------------ access

    @app.exception_handler(access.AccessRuleViolation)
    async def _rule(request: Request, exc: access.AccessRuleViolation) -> JSONResponse:
        return problem(request, 409, "Access rule violation", str(exc))

    @app.get("/v1/access/requirements", response_model=AccessRequirements, tags=["access"])
    def access_requirements(
        session: DB,
        asset_type: Literal["api", "data_product"],
        asset_id: Annotated[str, Query(pattern=r"^[a-z][a-z0-9-]{1,119}$")],
        version: Annotated[int | None, Query(ge=1, le=99)] = None,
    ) -> AccessRequirements:
        return access.requirements(session, asset_type, asset_id, version)

    @app.post("/v1/access-requests", response_model=access.AccessRequestOut, tags=["access"])
    def create_access_request(session: DB, body: access.AccessRequestIn, response: Response) -> access.AccessRequestOut:
        out = access.create(session, body)
        response.status_code = 201 if out.created else 200
        return out

    @app.get("/v1/access-requests", response_model=list[access.AccessRequestOut], tags=["access"])
    def list_access_requests(
        session: DB,
        requester: Annotated[str | None, Query(pattern=access.IDENTITY_PATTERN)] = None,
        status: Literal["pending_approval", "approved", "rejected", "withdrawn"] | None = None,
    ) -> list[access.AccessRequestOut]:
        return access.list_for(session, requester, status)

    @app.get("/v1/access-requests/{request_id}", response_model=access.AccessRequestOut, tags=["access"])
    def get_access_request(session: DB, request_id: uuid.UUID) -> access.AccessRequestOut:
        return access.get(session, request_id)

    @app.post("/v1/access-requests/{request_id}/decision", response_model=access.AccessRequestOut, tags=["access"])
    def decide_access_request(session: DB, request_id: uuid.UUID, body: access.DecisionIn) -> access.AccessRequestOut:
        """Human approval step. Phase 4 restricts this to tokens with an approver role."""
        return access.decide(session, request_id, body)

    return app


app = create_app()
