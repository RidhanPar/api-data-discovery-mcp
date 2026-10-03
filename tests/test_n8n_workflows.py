"""Structural tests for the exported n8n workflows (no n8n needed).

The end-to-end behaviour is exercised by scripts/demo_access_flow.py against the running
stack; these tests pin down the operational guarantees in the JSON itself.
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import re
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
WF_DIR = ROOT / "deploy" / "n8n" / "workflows"
spec = importlib.util.spec_from_file_location("build_workflows", ROOT / "deploy" / "n8n" / "build_workflows.py")
assert spec and spec.loader
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


def load(name: str) -> dict[str, Any]:
    return json.loads((WF_DIR / name).read_text(encoding="utf-8"))  # type: ignore[no-any-return]


ALL = sorted(p.name for p in WF_DIR.glob("*.json"))


def test_exported_json_matches_generator() -> None:
    for name, fn in build.WORKFLOWS.items():
        assert (WF_DIR / name).read_text(encoding="utf-8") == json.dumps(fn(), indent=2, ensure_ascii=False) + "\n", (
            f"{name} drifted; run deploy/n8n/build_workflows.py"
        )


@pytest.mark.parametrize("name", ALL)
def test_no_secrets_in_workflow_json(name: str) -> None:
    text = (WF_DIR / name).read_text(encoding="utf-8")
    assert "change-me" not in text
    assert not re.search(r"Bearer ey[A-Za-z0-9_-]{10,}", text)  # no hard-coded JWTs
    assert not re.search(r'"(client_secret|password)"\s*:\s*"[^={]', text)  # only $env expressions


@pytest.mark.parametrize("name", ALL)
def test_every_node_is_reachable_from_a_trigger(name: str) -> None:
    wf = load(name)
    names = {n["name"] for n in wf["nodes"]}
    triggers = {n["name"] for n in wf["nodes"] if n["type"].endswith(("webhook", "errorTrigger"))}
    seen, frontier = set(triggers), list(triggers)
    while frontier:
        for outputs in wf["connections"].get(frontier.pop(), {}).get("main", []):
            for edge in outputs:
                if edge["node"] not in seen:
                    seen.add(edge["node"])
                    frontier.append(edge["node"])
    assert names - seen == set()


def test_http_calls_have_timeouts_retries_and_error_routes() -> None:
    wf = load("access-request-approval.json")
    for n in wf["nodes"]:
        if n["type"] != "n8n-nodes-base.httpRequest":
            continue
        assert n["parameters"]["options"]["timeout"] <= 10000, n["name"]
        assert n.get("retryOnFail") and n.get("maxTries", 0) >= 3, n["name"]
        assert n.get("onError") == "continueErrorOutput", n["name"]
        error_edges = wf["connections"][n["name"]]["main"][1]
        assert [e["node"] for e in error_edges] == ["Describe failure"], n["name"]


def test_unhandled_failures_go_to_the_error_workflow() -> None:
    a = load("access-request-approval.json")
    z = load("error-handler.json")
    assert a["settings"]["errorWorkflow"] == z["id"]


def test_grant_happens_only_after_recorded_human_approval() -> None:
    """The only path to 'Grant role to requester' runs through the catalog decision and 'Approved?'."""
    wf = load("access-request-approval.json")
    parents: dict[str, set[str]] = {}
    for src, conn in wf["connections"].items():
        for idx, outputs in enumerate(conn.get("main", [])):
            for e in outputs:
                parents.setdefault(e["node"], set()).add(f"{src}#{idx}")
    # Walk upstream from the grant; every path must pass the true-branch of "Approved?".
    chain = ["Grant role to requester", "Get role", "Find requester in IdP"]
    for child, parent in itertools.pairwise(chain):
        assert parents[child] == {f"{parent}#0"}
    assert parents["Find requester in IdP"] == {"Approved?#0"}
    assert parents["Approved?"] == {"Record decision in catalog#0"}


def test_approval_wait_has_a_timeout() -> None:
    wf = load("access-request-approval.json")
    waits = [n for n in wf["nodes"] if n["type"] == "n8n-nodes-base.wait"]
    assert len(waits) == 2
    for w in waits:
        assert w["parameters"]["limitWaitTime"] is True
        assert "timeout_minutes" in w["parameters"]["resumeAmount"]


def test_links_and_webhook_are_verified_in_code() -> None:
    wf = load("access-request-approval.json")
    code = {n["name"]: n["parameters"].get("jsCode", "") for n in wf["nodes"]}
    assert "timingSafeEqual" in code["Verify signature & payload"]
    assert "<= 300" in code["Verify signature & payload"]  # replay window
    for stage in (1, 2):
        verify = code[f"Verify vote (stage {stage})"]
        assert "timingSafeEqual" in verify and "requester cannot approve own request" in verify
        assert "esc(r.justification)" in code[f"Sign links (stage {stage})"]  # HTML-escaped user text
