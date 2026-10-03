"""Async HTTP client for the catalog service, used by the MCP server.

Timeouts on every call, bounded retries with exponential backoff for transient
failures only (network errors, 502/503/504), and HTTP errors mapped to a small set of
stable error codes the MCP layer turns into structured tool errors.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from .guard import current_caller

TokenProvider = Callable[[], Awaitable[str]]

log = logging.getLogger(__name__)
RETRYABLE_STATUS = {502, 503, 504}


class CatalogError(Exception):
    """A failed catalog call, with a stable machine-readable code."""

    def __init__(self, code: str, message: str, *, retryable: bool = False, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.status = status


def _error_from(resp: httpx.Response) -> CatalogError:
    try:
        body = resp.json()
        detail = body.get("detail") or body.get("title") or resp.text
    except ValueError:
        detail = resp.text[:300]
    code = {
        401: "upstream_auth_error",
        403: "forbidden",
        404: "not_found",
        409: "rule_violation",
        422: "invalid_argument",
    }.get(resp.status_code, "upstream_error")
    return CatalogError(code, str(detail), retryable=resp.status_code >= 500, status=resp.status_code)


class CatalogClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 5.0,
        retries: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,
        token_provider: TokenProvider | None = None,
    ) -> None:
        self._token_provider = token_provider
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_s, connect=min(2.0, timeout_s)),
            transport=transport,
            headers={"User-Agent": "nordlys-mcp-server"},
        )
        self._retries = retries

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(
        self, method: str, path: str, *, params: Any = None, json: Any = None, request_id: str | None = None
    ) -> Any:
        headers = {"X-Request-ID": request_id} if request_id else {}
        caller = current_caller.get()
        if caller is not None:
            # Identity of the end user this call is made for; trusted only from allow-listed clients.
            headers["X-On-Behalf-Of-Subject"] = caller.subject
            headers["X-On-Behalf-Of-Scopes"] = " ".join(sorted(caller.all_scopes))
        if self._token_provider is not None:
            headers["Authorization"] = f"Bearer {await self._token_provider()}"
        for attempt in range(self._retries + 1):
            try:
                resp = await self._http.request(method, path, params=params, json=json, headers=headers)
            except httpx.TransportError as exc:
                if attempt == self._retries or method != "GET":
                    raise CatalogError(
                        "catalog_unavailable", f"catalog service unreachable: {exc}", retryable=True
                    ) from exc
            else:
                if resp.status_code in RETRYABLE_STATUS and attempt < self._retries and method == "GET":
                    log.warning("catalog %s %s -> %s, retrying", method, path, resp.status_code)
                elif resp.is_success:
                    return resp.json()
                else:
                    raise _error_from(resp)
            await asyncio.sleep(0.2 * 3**attempt)  # 0.2s, 0.6s, ...
        raise CatalogError("catalog_unavailable", "catalog service unavailable", retryable=True)  # pragma: no cover

    # ------------------------------------------------------------------ reads

    async def search(self, params: dict[str, Any]) -> Any:
        return await self._request("GET", "/v1/search", params={k: v for k, v in params.items() if v is not None})

    async def api_details(self, api_id: str, version: int) -> Any:
        return await self._request("GET", f"/v1/apis/{api_id}/v{version}")

    async def api_versions(self, api_id: str) -> Any:
        return await self._request("GET", f"/v1/apis/{api_id}")

    async def list_apis(self) -> Any:
        return await self._request("GET", "/v1/apis")

    async def endpoint_schema(self, api_id: str, version: int, method: str, path: str) -> Any:
        return await self._request(
            "GET", f"/v1/apis/{api_id}/v{version}/endpoint", params={"method": method, "path": path}
        )

    async def raw_spec(self, api_id: str, version: int) -> Any:
        return await self._request("GET", f"/v1/apis/{api_id}/v{version}/spec")

    async def compare(self, api_id: str, from_version: int, to_version: int) -> Any:
        return await self._request(
            "GET", f"/v1/apis/{api_id}/compare", params={"from_version": from_version, "to_version": to_version}
        )

    async def deprecations(self, sunset_before: str | None) -> Any:
        return await self._request(
            "GET", "/v1/deprecations", params={"sunset_before": sunset_before} if sunset_before else None
        )

    async def list_data_products(self) -> Any:
        return await self._request("GET", "/v1/data-products")

    async def data_product(self, product_id: str) -> Any:
        return await self._request("GET", f"/v1/data-products/{product_id}")

    # ------------------------------------------------------------------ access

    async def access_requirements(self, asset_type: str, asset_id: str, version: int | None) -> Any:
        params = {"asset_type": asset_type, "asset_id": asset_id, "version": version}
        return await self._request("GET", "/v1/access/requirements", params={k: v for k, v in params.items() if v})

    async def create_access_request(self, body: dict[str, Any]) -> Any:
        # POST is not retried automatically; the endpoint is idempotent, so the agent may safely retry.
        return await self._request("POST", "/v1/access-requests", json=body)

    async def sample_rows(self, product_id: str) -> Any:
        return await self._request("GET", f"/v1/data-products/{product_id}/samples")

    async def write_audit(self, event: dict[str, Any]) -> None:
        await self._request("POST", "/v1/audit-events", json=event)

    async def ready(self) -> bool:
        try:
            resp = await self._http.get("/health/ready")
            return resp.status_code == 200
        except httpx.TransportError:
            return False


class ClientCredentialsTokens:
    """OAuth 2.1 client-credentials tokens for service-to-service calls, cached until near expiry."""

    def __init__(self, token_url: str, client_id: str, client_secret: str, *, timeout_s: float = 5.0) -> None:
        self._url, self._id, self._secret = token_url, client_id, client_secret
        self._timeout = timeout_s
        self._token: str | None = None
        self._expires_at = 0.0

    async def __call__(self) -> str:
        if self._token and time.time() < self._expires_at - 30:
            return self._token
        async with httpx.AsyncClient(timeout=self._timeout) as http:
            resp = await http.post(
                self._url,
                data={"grant_type": "client_credentials", "client_id": self._id, "client_secret": self._secret},
            )
        if not resp.is_success:
            raise CatalogError(
                "upstream_auth_error",
                f"could not obtain a service token: HTTP {resp.status_code}",
                retryable=resp.status_code >= 500,
            )
        body = resp.json()
        self._token = str(body["access_token"])
        self._expires_at = time.time() + float(body.get("expires_in", 60))
        return self._token
