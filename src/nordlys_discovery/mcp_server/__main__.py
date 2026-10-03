"""Run the MCP server.

    uv run python -m nordlys_discovery.mcp_server                 # Streamable HTTP on :8001/mcp
    uv run python -m nordlys_discovery.mcp_server --transport stdio  # for clients that launch a process

The catalog service must be running (NORDLYS_CATALOG_API_URL, default http://localhost:8000).
With NORDLYS_AUTH_ENABLED=true every MCP request needs a bearer token from the configured
OIDC issuer (audience NORDLYS_MCP_AUDIENCE), and the server authenticates to the catalog
with its own client credentials.
"""

from __future__ import annotations

import argparse
import sys

from mcp.server.auth.settings import AuthSettings

from ..config import get_settings
from ..observability import configure_logging
from ..security.jwt import JwtValidator, McpTokenVerifier
from ..security.ratelimit import RateLimiter
from .catalog_client import CatalogClient, ClientCredentialsTokens
from .guard import Caller
from .server import build_server


def main() -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--transport", choices=["streamable-http", "stdio"], default="streamable-http")
    parser.add_argument("--host", default=settings.mcp_host)
    parser.add_argument("--port", type=int, default=settings.mcp_port)
    args = parser.parse_args()
    # stdio carries the protocol on stdout, so logs must go to stderr.
    configure_logging("mcp", settings.log_level, json_logs=settings.log_json, stream=sys.stderr)

    token_provider = None
    verifier = None
    auth = None
    if settings.auth_enabled:
        if args.transport == "stdio":
            parser.error("stdio is a local, single-user transport; run it with NORDLYS_AUTH_ENABLED=false")
        if settings.mcp_client_secret is None:
            parser.error("NORDLYS_MCP_CLIENT_SECRET is required when auth is enabled")
        token_url = (settings.oidc_jwks_url or settings.oidc_issuer + "/protocol/openid-connect/certs").replace(
            "/certs", "/token"
        )
        token_provider = ClientCredentialsTokens(
            token_url, settings.mcp_client_id, settings.mcp_client_secret.get_secret_value()
        )
        verifier = McpTokenVerifier(
            JwtValidator(issuer=settings.oidc_issuer, audience=settings.mcp_audience, jwks_url=settings.oidc_jwks_url)
        )
        auth = AuthSettings(
            issuer_url=settings.oidc_issuer,
            resource_server_url=settings.mcp_public_url,
            validate_token_resource=False,  # the verifier checks the audience itself
        )

    catalog = CatalogClient(
        settings.catalog_api_url,
        timeout_s=settings.catalog_timeout_s,
        retries=settings.catalog_retries,
        token_provider=token_provider,
    )
    server = build_server(
        catalog,
        dev_caller=Caller(settings.mcp_dev_subject, frozenset(settings.mcp_dev_asset_scopes)),
        token_verifier=verifier,
        auth_settings=auth,
        limiter=RateLimiter(settings.rate_limit_per_minute, settings.rate_limit_burst),
    )

    if args.transport == "stdio":
        server.run("stdio")
    else:
        server.run(
            "streamable-http",
            host=args.host,
            port=args.port,
            streamable_http_path="/mcp",
            max_request_body_size=64 * 1024,  # tool arguments are small; refuse anything bigger
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
