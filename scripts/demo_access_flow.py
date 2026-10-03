"""End-to-end demo of Workflow A against the running docker-compose stack (`make up`).

    uv run python scripts/demo_access_flow.py [--reject] [--asset claims-search-api]

1. alice (developer) gets a token from Keycloak and calls check_access over MCP: no access.
2. alice calls request_access over MCP -> the catalog stores a pending request and sends a
   signed event to n8n.
3. n8n e-mails the approver; this script reads the e-mail from Mailpit and "clicks" the
   Approve (or Reject) link, as bob would.
4. n8n records the decision in the catalog, grants the role in Keycloak, audits it and
   e-mails alice.
5. alice gets a NEW token and calls check_access again: access is now present.

Exits non-zero if any step does not behave as described.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import time
import uuid
from typing import Any

import httpx
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

KEYCLOAK = "http://localhost:8080/realms/nordlys/protocol/openid-connect/token"
MCP_URL = "http://localhost:8001/mcp"
MAILPIT = "http://localhost:8025/api/v1"


def user_token(username: str, password: str) -> str:
    r = httpx.post(
        KEYCLOAK,
        data={
            "grant_type": "password",
            "client_id": "nordlys-dev-cli",
            "username": username,
            "password": password,
            "scope": "openid",
        },
        timeout=10,
    )
    r.raise_for_status()
    return str(r.json()["access_token"])


async def call(token: str, tool: str, args: dict[str, Any]) -> Any:
    async with (
        httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30) as http,
        Client(streamable_http_client(MCP_URL, http_client=http)) as client,
    ):
        result = await client.call_tool(tool, args)
        if result.is_error:
            raise RuntimeError(f"{tool} failed: {result.content[0].text}")  # type: ignore[union-attr]
        return result.structured_content


def wait_for_mail(to: str, subject_contains: str, since: float, timeout_s: int = 60) -> dict[str, Any]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        msgs = httpx.get(f"{MAILPIT}/search", params={"query": f"to:{to}"}, timeout=10).json().get("messages", [])
        for m in msgs:
            created = m.get("Created", "")
            if subject_contains in m["Subject"] and created and _ts(created) >= since - 2:
                return httpx.get(f"{MAILPIT}/message/{m['ID']}", timeout=10).json()  # type: ignore[no-any-return]
        time.sleep(1)
    raise TimeoutError(f"no e-mail to {to} containing '{subject_contains}' within {timeout_s}s")


def _ts(iso: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def reset_grant(username: str, role: str) -> None:
    """Remove a previously granted realm role (local demo only; uses the Keycloak admin account)."""
    admin = httpx.post(
        "http://localhost:8080/realms/master/protocol/openid-connect/token",
        data={"grant_type": "password", "client_id": "admin-cli", "username": "admin", "password": "admin"},
        timeout=10,
    ).json()["access_token"]
    h = {"Authorization": f"Bearer {admin}"}
    base = "http://localhost:8080/admin/realms/nordlys"
    users = httpx.get(f"{base}/users", params={"username": username, "exact": "true"}, headers=h, timeout=10).json()
    r = httpx.get(f"{base}/roles/{role}", headers=h, timeout=10)
    if users and r.status_code == 200:
        httpx.request(
            "DELETE", f"{base}/users/{users[0]['id']}/role-mappings/realm", json=[r.json()], headers=h, timeout=10
        )
        print(f"reset: removed role {role} from {username}")


def step(n: int, text: str) -> None:
    print(f"\n[{n}] {text}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--reject", action="store_true", help="click Reject instead of Approve")
    p.add_argument("--asset", default="claims-search-api")
    p.add_argument("--version", type=int, default=1)
    p.add_argument("--tamper", action="store_true", help="edit the approver in the link (must be refused)")
    p.add_argument("--reset", action="store_true", help="first remove alice's role from a previous run")
    p.add_argument(
        "--no-click",
        action="store_true",
        help="nobody clicks: run n8n with APPROVAL_TIMEOUT_MINUTES=1 and expect expiry",
    )
    a = p.parse_args()
    if a.reset:
        reset_grant("alice", "claims.search")
    asset = {"asset_type": "api", "asset_id": a.asset, "version": a.version}

    step(1, "alice checks access over MCP")
    alice = user_token("alice", "alice-local")
    before = asyncio.run(call(alice, "check_access", asset))["decision"]
    print(f"    has_access={before['has_access']} missing={before['missing_scopes']}")
    if before["has_access"]:
        print(
            "    alice already has access (role granted in an earlier run). Restart keycloak to reset: "
            "docker compose up -d --force-recreate keycloak"
        )
        return 1

    step(2, "alice requests access over MCP")
    started = time.time()
    purpose = f"claims_reporting_{uuid.uuid4().hex[:6]}"  # unique so each demo run creates a new request
    out = asyncio.run(
        call(
            alice,
            "request_access",
            asset
            | {"purpose": purpose, "justification": "Daily backlog dashboard for the Norwegian claims handling team."},
        )
    )
    req = out["request"]
    print(f"    {out['outcome']}: request {req['id']} status={req['status']} approvers={req['approvers']}")

    step(3, "n8n e-mails the approver (read from Mailpit)")
    mail = wait_for_mail("bob@nordlys.example", "Access request", started)
    links = dict(re.findall(r'href="([^"]+decision=(approved|rejected)[^"]*)"', mail["HTML"]))
    by_decision = {d: url.replace("&amp;", "&") for url, d in links.items()}
    print(f"    subject: {mail['Subject']}")
    if a.no_click:
        step(4, "nobody clicks; the approval window expires")
        note = wait_for_mail("alice@nordlys.example", "Your access request", started, timeout_s=180)
        body = httpx.get(f"{MAILPIT}/message/{note['ID']}", timeout=10).json()["Text"]
        print(f"    alice receives: {note['Subject']} | {' '.join(body.split())[:90]}")
        return 0 if "rejected" in note["Subject"] and "Expired" in body else 1

    decision = "rejected" if a.reject else "approved"
    url = by_decision[decision]
    if a.tamper:
        url = url.replace("approver=bob%40nordlys.example", "approver=mallory%40nordlys.example")
        print("    an attacker edits the link to approve as mallory")
    else:
        print(f"    bob clicks {decision.upper()}")
    click = httpx.get(url, timeout=20)
    print(f"    -> HTTP {click.status_code}: {click.text[:80]}")

    if a.tamper:
        step(4, "n8n must refuse the tampered link and alert ops, deciding nothing")
        alert = wait_for_mail("platform-ops@nordlys.example", "needs attention", started)
        print(f"    ops receives: {alert['Subject']}")
        after = asyncio.run(call(user_token("alice", "alice-local"), "check_access", asset))["decision"]
        print(f"    has_access={after['has_access']} (request stays pending)")
        return 0 if not after["has_access"] else 1

    step(4, "n8n records the decision, applies the grant, notifies alice")
    note = wait_for_mail("alice@nordlys.example", "Your access request", started, timeout_s=90)
    print(f"    alice receives: {note['Subject']}")

    step(5, "alice gets a new token and checks access again")
    after = asyncio.run(call(user_token("alice", "alice-local"), "check_access", asset))["decision"]
    print(f"    has_access={after['has_access']}")
    expected = not a.reject
    if after["has_access"] != expected:
        print(f"FAILED: expected has_access={expected}")
        return 1
    print("\nOK: access changed only through a human decision, end to end.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
