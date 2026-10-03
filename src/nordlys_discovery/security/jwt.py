"""JWT validation for OAuth 2.1 bearer tokens (Keycloak locally, Entra ID in Azure).

Checks signature (RS256/ES256 keys from the issuer's JWKS), issuer, audience, expiry and
not-before. Keys are cached and refreshed on an unknown `kid`, so key rotation works
without restarts. `alg=none` and HMAC algorithms are rejected outright, which closes
the classic algorithm-confusion attacks.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import anyio
import jwt
from jwt import PyJWKClient
from mcp.server.auth.provider import AccessToken
from pydantic import BaseModel

log = logging.getLogger(__name__)
ALLOWED_ALGORITHMS = ["RS256", "RS384", "RS512", "ES256", "PS256"]


class Principal(BaseModel):
    """The authenticated caller, independent of the identity provider."""

    subject: str  # stable id (sub)
    username: str  # human-readable (preferred_username / upn / email)
    client_id: str  # the OAuth client that obtained the token (azp / appid)
    scopes: frozenset[str]
    claims: dict[str, Any]


class TokenError(Exception):
    pass


def _scopes(claims: dict[str, Any]) -> frozenset[str]:
    # Keycloak/RFC 8693: space-separated `scope`. Entra ID: `scp` (delegated) or `roles` (app).
    raw = claims.get("scope") or claims.get("scp") or ""
    scopes = set(raw.split()) if isinstance(raw, str) else set(raw)
    roles = claims.get("roles")
    if isinstance(roles, list):
        scopes.update(str(r) for r in roles)
    return frozenset(scopes)


class JwtValidator:
    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_url: str | None = None,
        leeway_s: int = 30,
        key_resolver: Callable[[str], Any] | None = None,
    ) -> None:
        self.issuer = issuer.rstrip("/")
        self.audience = audience
        self.leeway_s = leeway_s
        if key_resolver is None:
            client = PyJWKClient(
                jwks_url or f"{self.issuer}/protocol/openid-connect/certs", cache_keys=True, lifespan=600, timeout=5
            )
            key_resolver = lambda token: client.get_signing_key_from_jwt(token).key  # noqa: E731
        self._resolve_key = key_resolver

    def validate(self, token: str) -> Principal:
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") not in ALLOWED_ALGORITHMS:
                raise TokenError(f"algorithm {header.get('alg')!r} not allowed")
            claims: dict[str, Any] = jwt.decode(
                token,
                key=self._resolve_key(token),
                algorithms=ALLOWED_ALGORITHMS,
                audience=self.audience,
                issuer=self.issuer,
                leeway=self.leeway_s,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except TokenError:
            raise
        except jwt.PyJWTError as exc:
            raise TokenError(str(exc)) from exc
        return Principal(
            subject=str(claims["sub"]),
            username=str(claims.get("preferred_username") or claims.get("upn") or claims.get("email") or claims["sub"]),
            client_id=str(claims.get("azp") or claims.get("appid") or claims.get("client_id") or "unknown"),
            scopes=_scopes(claims),
            claims=claims,
        )


class McpTokenVerifier:
    """Adapter to the MCP SDK's TokenVerifier protocol."""

    def __init__(self, validator: JwtValidator) -> None:
        self._validator = validator

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            # JWKS fetches are blocking I/O; keep them off the event loop.
            p = await anyio.to_thread.run_sync(self._validator.validate, token)
        except TokenError as exc:
            log.info("rejected bearer token: %s", exc)
            return None
        return AccessToken(
            token=token,
            client_id=p.client_id,
            scopes=sorted(p.scopes),
            expires_at=int(p.claims["exp"]),
            resource=self._validator.audience,
            subject=p.subject,
            claims=p.claims,
        )


def principal_from_access_token(token: AccessToken) -> Principal:
    claims = token.claims or {}
    return Principal(
        subject=token.subject or str(claims.get("sub", "unknown")),
        username=str(claims.get("preferred_username") or claims.get("upn") or claims.get("email") or token.subject),
        client_id=token.client_id,
        scopes=frozenset(token.scopes),
        claims=claims,
    )
