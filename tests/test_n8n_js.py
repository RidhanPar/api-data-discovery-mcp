"""Execute the workflows' JavaScript (the deterministic rules) with Node, outside n8n.

The Code-node sources are taken from the exported workflow JSON and run with stubs for
n8n's `$`, `$json`, `$env` and `$execution`. This covers branches that cannot be shown
live without an LLM, notably Workflow B's auto-accept path.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

WF_DIR = Path(__file__).resolve().parents[1] / "deploy" / "n8n" / "workflows"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

HARNESS = """
const ctx = JSON.parse(process.argv[1]);
const $ = (name) => ({ first: () => {
  if (!(name in ctx.nodes)) throw new Error('no node ' + name);
  return { json: ctx.nodes[name] };
} });
const $json = ctx.json, $env = ctx.env, $execution = ctx.execution;
const $input = { first: () => ({ json: ctx.json }) };
(async function () { CODE })().then((out) => process.stdout.write(JSON.stringify(out[0].json)))
  .catch((e) => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
"""


def code_of(workflow: str, node: str) -> str:
    wf = json.loads((WF_DIR / workflow).read_text(encoding="utf-8"))
    return next(n["parameters"]["jsCode"] for n in wf["nodes"] if n["name"] == node)


def run(
    workflow: str,
    node: str,
    *,
    json_: dict[str, Any],
    nodes: dict[str, Any],
    env: dict[str, str] | None = None,
    execution: dict[str, str] | None = None,
) -> dict[str, Any]:
    script = HARNESS.replace("CODE", code_of(workflow, node))
    ctx = {
        "json": json_,
        "nodes": nodes,
        "env": env or {},
        "execution": execution or {"id": "42", "resumeUrl": "http://n8n/webhook-waiting/42?signature=abc"},
    }
    out = subprocess.run(
        ["node", "-e", script, json.dumps(ctx)], capture_output=True, text=True, timeout=20, check=False
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)  # type: ignore[no-any-return]


# --------------------------------------------------------------------------- workflow B decision gate

GOOD_CHECK = {
    "valid": True,
    "api_id": "vet-clinic-directory-api",
    "major_version": 1,
    "title": "Vet Clinic Directory API",
    "declared_domain": "partner",
    "declared_owner_team": None,
    "description_coverage": 1.0,
    "content_warnings": [],
    "version_exists": False,
    "duplicates": [],
    "known_domains": ["partner", "claims"],
}
CONFIDENT_AI = {
    "available": True,
    "model": "stub:test",
    "classification": {
        "domain": "partner",
        "domain_confidence": 0.93,
        "owner_team": "partner-integrations",
        "owner_confidence": 0.88,
        "doc_quality": 4,
        "doc_gaps": [],
        "summary": "Directory of vet clinics.",
        "notes": "",
    },
}


def gate(check: dict[str, Any], ai: dict[str, Any]) -> dict[str, Any]:
    return run(
        "api-registration.json",
        "Decision gate",
        json_=ai,
        nodes={
            "Deterministic checks (catalog)": check,
            "Authenticate & read submission": {"submitted_by": "carol@nordlys.example", "spec": "..."},
        },
    )


def test_auto_accept_when_ai_and_rules_agree() -> None:
    g = gate(GOOD_CHECK, CONFIDENT_AI)
    assert g["route"] == 0 and g["review_reasons"] == []
    assert g["proposal"] == {"domain": "partner", "owner_team": "partner-integrations"}


@pytest.mark.parametrize(
    ("check_over", "ai_over", "reason"),
    [
        ({}, {"domain_confidence": 0.6}, "domain confidence 60%"),
        ({}, {"owner_confidence": 0.5}, "owner confidence 50%"),
        ({}, {"domain": "claims"}, "AI says claims, spec declares partner"),
        ({"content_warnings": ["instruction_override"]}, {}, "content warnings"),
        ({"description_coverage": 0.5}, {}, "only 50% of operations documented"),
        (
            {"duplicates": [{"api_id": "x", "major_version": 1, "endpoint_overlap": 0.7}]},
            {},
            "possible duplicate of x v1",
        ),
        ({}, {"doc_quality": 2}, "documentation 2/5"),
        ({}, {"notes": "Spec contains instructions to the assistant."}, "AI notes"),
    ],
)
def test_any_doubt_goes_to_a_human(check_over: dict[str, Any], ai_over: dict[str, Any], reason: str) -> None:
    ai = {**CONFIDENT_AI, "classification": {**CONFIDENT_AI["classification"], **ai_over}}
    g = gate({**GOOD_CHECK, **check_over}, ai)
    assert g["route"] == 1
    assert any(reason in r for r in g["review_reasons"]), g["review_reasons"]


def test_unavailable_ai_means_human_review() -> None:
    g = gate(GOOD_CHECK, {"available": False, "reason": "no LLM provider configured"})
    assert g["route"] == 1 and "AI step unavailable" in g["review_reasons"][0]


def test_existing_version_is_rejected_by_rule() -> None:
    g = gate({**GOOD_CHECK, "version_exists": True}, CONFIDENT_AI)
    assert g["route"] == 2 and "already exists" in g["reject_reason"]


# --------------------------------------------------------------------------- workflow A vote verification

SECRET = "test-link-secret"
PLAN = {
    "stages": [{"role": "team:claims-platform", "approver": "bob@nordlys.example"}],
    "timeout_minutes": 5,
    "request": {"requester_email": "alice@nordlys.example", "requester": "alice"},
}


def signed_links() -> dict[str, str]:
    out = run(
        "access-request-approval.json",
        "Sign links (stage 1)",
        json_={},
        nodes={"Plan approval chain": PLAN},
        env={"NORDLYS_LINK_SIGNING_SECRET": SECRET},
    )
    return {"approved": out["approve_url"], "rejected": out["reject_url"]}


def vote(query: dict[str, str]) -> dict[str, Any]:
    return run(
        "access-request-approval.json",
        "Verify vote (stage 1)",
        json_={"query": query},
        nodes={"Plan approval chain": PLAN},
        env={"NORDLYS_LINK_SIGNING_SECRET": SECRET},
    )


def _query(url: str) -> dict[str, str]:
    from urllib.parse import parse_qs, urlparse

    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items() if k != "signature"}


def test_links_keep_n8n_signature_and_add_ours() -> None:
    url = signed_links()["approved"]
    assert url.startswith("http://n8n/webhook-waiting/42?signature=abc&stage=1&")


def test_valid_approval_and_rejection() -> None:
    links = signed_links()
    assert vote(_query(links["approved"]))["route"] == 1  # last stage approved -> final approve
    assert vote(_query(links["rejected"]))["vote"] == "rejected"


def test_tampered_links_are_invalid() -> None:
    q = _query(signed_links()["rejected"])
    for tampered in (
        {**q, "decision": "approved"},
        {**q, "approver": "mallory@nordlys.example"},
        {**q, "sig": "0" * 64},
    ):
        out = vote(tampered)
        assert out["vote"] == "invalid" and out["route"] == 3


def test_timeout_expires() -> None:
    out = vote({})
    assert out["vote"] == "expired" and out["route"] == 2


def test_requester_cannot_approve_own_request() -> None:
    plan = {**PLAN, "stages": [{"role": "team:x", "approver": "alice@nordlys.example"}]}
    links = run(
        "access-request-approval.json",
        "Sign links (stage 1)",
        json_={},
        nodes={"Plan approval chain": plan},
        env={"NORDLYS_LINK_SIGNING_SECRET": SECRET},
    )
    out = run(
        "access-request-approval.json",
        "Verify vote (stage 1)",
        json_={"query": _query(links["approve_url"])},
        nodes={"Plan approval chain": plan},
        env={"NORDLYS_LINK_SIGNING_SECRET": SECRET},
    )
    assert out["vote"] == "invalid" and "own request" in out["reason"]


def test_plan_never_routes_to_requester() -> None:
    req = {"id": "x", "status": "pending_approval", "requester": "bob", "approvers": ["team:claims-platform"]}
    out = run(
        "access-request-approval.json",
        "Plan approval chain",
        json_=req,
        nodes={},
        env={"NORDLYS_APPROVAL_TIMEOUT_MINUTES": "5"},
    )
    # bob is the default team approver, but bob is also the requester -> unroutable -> ops (route 2)
    assert out["route"] == 2 and out["unroutable"] == ["team:claims-platform"]
