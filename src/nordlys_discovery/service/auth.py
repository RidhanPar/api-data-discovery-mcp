"""Authentication for the catalog service.

The catalog is an internal service. Only trusted platform services call it (the MCP
server and n8n), each with its own client-credentials token (audience nordlys-catalog,
scope catalog.internal). A trusted service states which end user it acts for in
X-On-Behalf-Of-* headers. The catalog accepts those headers only from clients on the
allow-list, so a random internal workload cannot impersonate users.

Why not forward the user's own token? The MCP specification forbids token passthrough:
a token issued for the MCP server must not be replayed to another API. The production
pattern is a token exchange (RFC 8693; Entra ID's on-behalf-of flow) that mints a
catalog-audience token carrying the user's identity. ADR-0004 discusses the trade-off.
With auth disabled (unit tests, quick local runs), the headers are trusted as-is.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, Request

from ..config import Settings
from ..security.jwt import JwtValidator, TokenError

OBO_SUBJECT = "X-On-Behalf-Of-Subject"
OBO_SCOPES = "X-On-Behalf-Of-Scopes"


@dataclass(frozen=True)
class CallContext:
    service: str  # authenticated calling service (client id), or "local-dev"
    user: str  # end user the service acts for
    user_scopes: frozenset[str]

    def has(self, scope: str) -> bool:
        return scope in self.user_scopes


class CatalogAuth:
    def __init__(self, settings: Settings, validator: JwtValidator | None = None) -> None:
        self.enabled = settings.auth_enabled
        self.trusted = set(settings.trusted_service_clients)
        self.validator = validator
        if self.enabled and validator is None:
            self.validator = JwtValidator(
                issuer=settings.oidc_issuer, audience=settings.catalog_audience, jwks_url=settings.oidc_jwks_url
            )

    def __call__(self, request: Request) -> CallContext:
        user = request.headers.get(OBO_SUBJECT, "").strip()
        scopes = frozenset(request.headers.get(OBO_SCOPES, "").split())
        if not self.enabled:
            ctx = CallContext("local-dev", user or "dev.user@nordlys.example", scopes)
            request.state.call_context = ctx
            return ctx

        header = request.headers.get("Authorization", "")
        if not header.lower().startswith("bearer "):
            raise HTTPException(401, "Bearer token required")
        assert self.validator is not None
        try:
            principal = self.validator.validate(header[7:].strip())
        except TokenError as exc:
            raise HTTPException(401, f"Invalid token: {exc}") from exc
        if "catalog.internal" not in principal.scopes:
            raise HTTPException(403, "Token lacks scope catalog.internal")
        if user:
            if principal.client_id not in self.trusted:
                raise HTTPException(403, f"Client {principal.client_id} may not act on behalf of users")
            ctx = CallContext(principal.client_id, user, scopes)
        else:
            # The service acts as itself (e.g. n8n housekeeping): only its own token scopes count.
            ctx = CallContext(principal.client_id, principal.username, principal.scopes)
        request.state.call_context = ctx
        return ctx
