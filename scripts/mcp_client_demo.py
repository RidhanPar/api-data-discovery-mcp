"""MCP client demo: answer "Which API gives me open claims for Norway, and how do I get access?"

    uv run python scripts/mcp_client_demo.py [--url http://localhost:8001/mcp] [--user alice] [--no-auth]

Against the docker-compose stack (`make up`) it signs in as a demo user through Keycloak
(OAuth password grant on the public dev client, local demo only). Use --no-auth against
`make mcp`, which runs without an identity provider.

A scripted walk through the tools over Streamable HTTP, with no LLM involved. It shows
exactly what an AI assistant sees when it uses the server. Phase 5 replaces the script
with an agent that decides which tools to call.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

import httpx
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from nordlys_discovery.mcp_server.server import parse_tool_error


def show(title: str, data: Any) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(data, indent=2, default=str)[:2500])


async def call(client: Client, tool: str, args: dict[str, Any]) -> Any:
    result = await client.call_tool(tool, args)
    if result.is_error:
        text = result.content[0].text if result.content else ""  # type: ignore[union-attr]
        return {"tool_error": parse_tool_error(text) or text}
    return result.structured_content


TOKEN_URL = "http://localhost:8080/realms/nordlys/protocol/openid-connect/token"


def user_token(username: str, password: str) -> str:
    r = httpx.post(
        TOKEN_URL,
        data={"grant_type": "password", "client_id": "nordlys-dev-cli", "username": username, "password": password},
        timeout=10,
    )
    r.raise_for_status()
    return str(r.json()["access_token"])


async def main(url: str, token: str | None) -> None:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with (
        httpx2.AsyncClient(headers=headers, timeout=30) as http,
        Client(streamable_http_client(url, http_client=http)) as client,
    ):
        tools = await client.list_tools()
        print("Tools:", ", ".join(t.name for t in tools.tools))
        templates = await client.list_resource_templates()
        print("Resource templates:", ", ".join(t.uri_template for t in templates.resource_templates))
        prompts = await client.list_prompts()
        print("Prompts:", ", ".join(p.name for p in prompts.prompts))

        # 1. Discover.
        found = await call(
            client,
            "search_catalog",
            {"query": "open claims in Norway", "country": "NO", "asset_type": "api", "limit": 3},
        )
        show(
            "search_catalog",
            [{k: r[k] for k in ("rank", "citation", "deprecated", "replacement")} for r in found["results"]],
        )
        top = next(r for r in found["results"] if not r["deprecated"])

        # 2. Understand.
        details = await call(client, "get_api_details", {"api_id": top["asset_id"], "version": top["version"]})
        show(
            "get_api_details",
            {k: details[k] for k in ("api_id", "major_version", "owner_team", "lifecycle_status", "scopes")}
            | {"endpoints": [f"{e['method']} {e['path']}" for e in details["endpoints"]]},
        )
        schema = await call(
            client,
            "get_endpoint_schema",
            {"api_id": top["asset_id"], "version": top["version"], "method": "GET", "path": "/claims"},
        )
        show("get_endpoint_schema (parameters)", [(p["name"], p.get("description")) for p in schema["parameters"]])

        # 3. Access.
        access = await call(
            client, "check_access", {"asset_type": "api", "asset_id": top["asset_id"], "version": top["version"]}
        )
        show("check_access", access["decision"] | {"requirements": "..."})
        req = await call(
            client,
            "request_access",
            {
                "asset_type": "api",
                "asset_id": top["asset_id"],
                "version": top["version"],
                "purpose": "claims_operations_reporting",
                "justification": "Daily backlog dashboard for the Norwegian claims handling team.",
            },
        )
        show("request_access", req)

        # 4. Guard rails: the look-alike BFF and a prohibited data-product purpose.
        show(
            "request_access on the channel BFF (expected: refused)",
            await call(
                client,
                "request_access",
                {
                    "asset_type": "api",
                    "asset_id": "claim-lookup-api",
                    "version": 1,
                    "purpose": "claims_operations_reporting",
                    "justification": "Trying the look-alike API to see what happens.",
                },
            ),
        )
        show(
            "check_access fraud scores for marketing (expected: purpose not allowed)",
            (
                await call(
                    client,
                    "check_access",
                    {"asset_type": "data_product", "asset_id": "claims-fraud-scores", "purpose": "marketing"},
                )
            )["decision"]["explanation"],
        )
        show(
            "validation error (expected: rejected before tool code runs)",
            await call(client, "get_api_details", {"api_id": "Claims API!!", "version": 2}),
        )

        index = await client.read_resource("nordlys://catalog/index")
        print(f"\nResource nordlys://catalog/index: {len(index.contents[0].text)} bytes")  # type: ignore[union-attr]


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default="http://localhost:8001/mcp")
    p.add_argument("--user", default="dpo", help="demo user (alice, bob, dpo)")
    p.add_argument("--password", default=None, help="default: <user>-local, the local realm's demo password")
    p.add_argument("--no-auth", action="store_true")
    a = p.parse_args()
    asyncio.run(main(a.url, None if a.no_auth else user_token(a.user, a.password or f"{a.user}-local")))
