"""Run the MCP server.

    uv run python -m nordlys_discovery.mcp_server                 # Streamable HTTP on :8001/mcp
    uv run python -m nordlys_discovery.mcp_server --transport stdio  # for clients that launch a process

The catalog service must be running (NORDLYS_CATALOG_API_URL, default http://localhost:8000).
"""

from __future__ import annotations

import argparse
import logging
import sys

from ..config import get_settings
from .catalog_client import CatalogClient
from .server import Caller, build_server


def main() -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--transport", choices=["streamable-http", "stdio"], default="streamable-http")
    parser.add_argument("--host", default=settings.mcp_host)
    parser.add_argument("--port", type=int, default=settings.mcp_port)
    args = parser.parse_args()
    # stdio carries the protocol on stdout, so logs must go to stderr.
    logging.basicConfig(level=settings.log_level, stream=sys.stderr)

    catalog = CatalogClient(
        settings.catalog_api_url, timeout_s=settings.catalog_timeout_s, retries=settings.catalog_retries
    )
    dev_caller = Caller(settings.mcp_dev_subject, frozenset(settings.mcp_dev_asset_scopes))
    server = build_server(catalog, lambda: dev_caller)

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
