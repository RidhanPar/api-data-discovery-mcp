"""End-to-end demo of Workflow B against the running stack (`make up`).

    uv run python scripts/demo_registration_flow.py --scenario good|malicious|invalid|duplicate

good       well-documented new API -> checks pass -> AI step -> review (or auto) -> published,
           then it is findable through MCP search_catalog
malicious  partner spec with prompt-injection text -> flagged -> reviewer rejects
invalid    not OpenAPI 3.1 -> rejected by deterministic checks, no AI call
duplicate  re-submits an existing API version -> rejected by rule, no human needed

With no LLM configured (NORDLYS_LLM_PROVIDER=none) the AI step reports itself unavailable
and every valid registration goes to human review. That is the designed fallback.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from demo_access_flow import MAILPIT, call, user_token, wait_for_mail

ROOT = Path(__file__).resolve().parents[1]
WEBHOOK = "http://localhost:5678/webhook/api-registration"
API_KEY = "local-registration-key-change-me"
SUBMITTER = "carol.partner@nordlys.example"


def submit(spec: str) -> httpx.Response:
    return httpx.post(
        WEBHOOK, json={"submitted_by": SUBMITTER, "spec": spec}, headers={"X-API-Key": API_KEY}, timeout=30
    )


def click(mail: dict[str, Any], decision: str, domain: str = "partner") -> None:
    links = [u.replace("&amp;", "&") for u in re.findall(r'href="([^"]+)"', mail["HTML"])]
    accept = [u for u in links if "decision=accepted" in u]
    chosen = next((u for u in accept if f"domain={domain}" in u), accept[0] if accept else None)
    url = chosen if decision == "accepted" else next(u for u in links if "decision=rejected" in u)
    assert url is not None, "no matching link in the review e-mail"
    r = httpx.get(url, timeout=20)
    dom = re.search(r"domain=([a-z]*)", url)
    label = f"ACCEPT into {dom.group(1) if dom else '?'}" if decision == "accepted" else "REJECT"
    print(f"    reviewer clicks {label} -> HTTP {r.status_code}")


def text_of(mail: dict[str, Any]) -> str:
    return " ".join(httpx.get(f"{MAILPIT}/message/{mail['ID']}", timeout=10).json()["Text"].split())


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--scenario", choices=["good", "malicious", "invalid", "duplicate"], default="good")
    a = p.parse_args()
    started = time.time()

    if a.scenario == "invalid":
        r = submit("openapi: 3.0.0\ninfo: {title: Legacy thing}\npaths: {}\n")
        print(f"[1] submitted -> HTTP {r.status_code} {r.json()}")
        m = wait_for_mail(SUBMITTER, "could not be registered", started)
        print(f"[2] submitter told: {m['Subject']} | {text_of(m)[:120]}")
        return 0

    if a.scenario == "duplicate":
        spec = (ROOT / "catalog/apis/claims/claims-api.v2.yaml").read_text(encoding="utf-8")
        r = submit(spec)
        print(f"[1] re-submitted claims-api v2 -> HTTP {r.status_code}")
        m = wait_for_mail(SUBMITTER, "not published", started)
        print(f"[2] submitter told: {m['Subject']} | {text_of(m)[:140]}")
        return 0 if "already exists" in text_of(m) else 1

    fixture = "vet-clinic-directory-api.v1.yaml" if a.scenario == "good" else "malicious-partner-api.v1.yaml"
    r = submit((ROOT / "tests/fixtures" / fixture).read_text(encoding="utf-8"))
    print(f"[1] submitted {fixture} -> HTTP {r.status_code} {r.json()}")

    try:
        review = wait_for_mail("api-governance@nordlys.example", "Review API registration", started, timeout_s=60)
    except TimeoutError:
        review = None
    if review:
        body = text_of(review)
        why = body.split("Why you are asked:")[1].split("Checks")[0].strip() if "Why you are asked:" in body else "?"
        print(f"[2] reviewer e-mailed. Why: {why[:300]}")
        click(review, "accepted" if a.scenario == "good" else "rejected")
    else:
        print("[2] no review needed: all checks and the AI agreed (auto-accept path)")

    outcome = wait_for_mail(SUBMITTER, "API", started, timeout_s=90)
    print(f"[3] submitter told: {outcome['Subject']}")
    if a.scenario == "malicious":
        return 0 if "not published" in outcome["Subject"] else 1

    found = __import__("asyncio").run(
        call(user_token("alice", "alice-local"), "search_catalog", {"query": "vet clinic direct billing", "limit": 3})
    )
    hits = [h["citation"] for h in found["results"]]
    print(f"[4] MCP search 'vet clinic direct billing' -> {hits}")
    return 0 if any(h.startswith("vet-clinic-directory-api") for h in hits) else 1


if __name__ == "__main__":
    sys.exit(main())
