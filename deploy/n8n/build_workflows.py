"""Generate the n8n workflow JSON files in deploy/n8n/workflows/.

    uv run python deploy/n8n/build_workflows.py          # write
    uv run python deploy/n8n/build_workflows.py --check  # CI: fail if the JSON drifted

The workflows are generated rather than hand-edited so that every JavaScript step
is reviewable code and the node layout stays tidy. The JSON is what n8n imports;
after editing in the n8n UI, export it back over the generated file (and port the
change here).
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any

OUT = Path(__file__).parent / "workflows"
NS = uuid.UUID("6f1d2c3b-0000-4000-8000-00000000a11c")  # stable ids across regenerations

JSON = dict[str, Any]

# --------------------------------------------------------------------------- node helpers


def _id(workflow: str, name: str) -> str:
    return str(uuid.uuid5(NS, f"{workflow}/{name}"))


class Flow:
    def __init__(self, name: str, slug: str) -> None:
        self.name, self.slug = name, slug
        self.nodes: list[JSON] = []
        self.connections: dict[str, JSON] = {}

    def node(self, name: str, type_: str, version: float, pos: tuple[int, int], params: JSON, **extra: Any) -> str:
        n: JSON = {
            "id": _id(self.slug, name),
            "name": name,
            "type": type_,
            "typeVersion": version,
            "position": list(pos),
            "parameters": params,
        }
        n.update(extra)
        self.nodes.append(n)
        return name

    def link(self, src: str, dst: str, output: int = 0) -> None:
        outs = self.connections.setdefault(src, {"main": []})["main"]
        while len(outs) <= output:
            outs.append([])
        outs[output].append({"node": dst, "type": "main", "index": 0})

    def code(self, name: str, pos: tuple[int, int], js: str, note: str) -> str:
        return self.node(
            name, "n8n-nodes-base.code", 2, pos, {"jsCode": js.strip() + "\n"}, notes=note, notesInFlow=True
        )

    def http(
        self,
        name: str,
        pos: tuple[int, int],
        *,
        method: str,
        url: str,
        note: str,
        headers: list[tuple[str, str]] | None = None,
        json_body: str | None = None,
        form: list[tuple[str, str]] | None = None,
        never_error: bool = False,
        error_output: bool = True,
    ) -> str:
        p: JSON = {"method": method, "url": url, "options": {"timeout": 10000}}
        if headers:
            p["sendHeaders"] = True
            p["headerParameters"] = {"parameters": [{"name": k, "value": v} for k, v in headers]}
        if json_body is not None:
            p |= {"sendBody": True, "specifyBody": "json", "jsonBody": json_body}
        if form is not None:
            p |= {
                "sendBody": True,
                "contentType": "form-urlencoded",
                "bodyParameters": {"parameters": [{"name": k, "value": v} for k, v in form]},
            }
        if never_error:
            p["options"]["response"] = {"response": {"neverError": True, "fullResponse": True}}
        extra: JSON = {"retryOnFail": True, "maxTries": 3, "waitBetweenTries": 2000, "notes": note, "notesInFlow": True}
        if error_output:
            extra["onError"] = "continueErrorOutput"
        return self.node(name, "n8n-nodes-base.httpRequest", 4.2, pos, p, **extra)

    def email(self, name: str, pos: tuple[int, int], *, to: str, subject: str, html: str, note: str) -> str:
        return self.node(
            name,
            "n8n-nodes-base.emailSend",
            2.1,
            pos,
            {
                "fromEmail": "discovery-platform@nordlys.example",
                "toEmail": to,
                "subject": subject,
                "emailFormat": "html",
                "html": html,
                "options": {"appendAttribution": False},
            },
            credentials={"smtp": {"id": "mailpit-smtp", "name": "Mailpit SMTP (local)"}},
            retryOnFail=True,
            maxTries=3,
            waitBetweenTries=2000,
            notes=note,
            notesInFlow=True,
        )

    def if_true(self, name: str, pos: tuple[int, int], expr: str, note: str) -> str:
        return self.node(
            name,
            "n8n-nodes-base.if",
            2.2,
            pos,
            {
                "conditions": {
                    "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
                    "conditions": [
                        {
                            "id": _id(self.slug, name + "/c"),
                            "leftValue": expr,
                            "rightValue": "",
                            "operator": {"type": "boolean", "operation": "true", "singleValue": True},
                        }
                    ],
                    "combinator": "and",
                },
                "options": {},
            },
            notes=note,
            notesInFlow=True,
        )

    def switch(self, name: str, pos: tuple[int, int], expr: str, outputs: int, note: str) -> str:
        return self.node(
            name,
            "n8n-nodes-base.switch",
            3.2,
            pos,
            {"mode": "expression", "numberOutputs": outputs, "output": expr},
            notes=note,
            notesInFlow=True,
        )

    def layout(self, grid: dict[str, tuple[int, int]], dx: int = 240, dy: int = 200) -> None:
        """Place nodes on a readable grid of lanes (column, row)."""
        for n in self.nodes:
            if n["name"] in grid:
                col, row = grid[n["name"]]
                n["position"] = [col * dx, row * dy]

    def to_json(self, settings: JSON | None = None) -> JSON:
        return {
            "id": str(uuid.uuid5(NS, self.slug)).replace("-", "")[:16],
            "name": self.name,
            "active": False,
            "nodes": self.nodes,
            "connections": self.connections,
            "settings": {"executionOrder": "v1", "saveManualExecutions": True, **(settings or {})},
            "pinData": {},
            "tags": [],
        }


# --------------------------------------------------------------------------- shared JS

ENV = "const env = $env;"
SIGN_JS = """
// [rule] One approve and one reject link per approver. Each link carries an HMAC over
// (execution id, stage, approver, decision), so a recipient cannot edit the URL to approve
// as someone else or at another stage. The resume URL itself is unguessable per execution.
const crypto = require('crypto');
const plan = $('Plan approval chain').first().json;
const stage = plan.stages[STAGE - 1];
const secret = $env.NORDLYS_LINK_SIGNING_SECRET;
const sign = (d) => crypto.createHmac('sha256', secret)
  .update([$execution.id, STAGE, stage.approver, d].join('|')).digest('hex');
// n8n 2.x resume URLs already carry their own ?signature=..., so append with '&'.
const base = $execution.resumeUrl;
const link = (d) => `${base}${base.includes('?') ? '&' : '?'}stage=${STAGE}&approver=${encodeURIComponent(stage.approver)}&decision=${d}&sig=${sign(d)}`;
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const r = plan.request;
return [{ json: {
  approver: stage.approver, role: stage.role, stage: STAGE,
  approve_url: link('approved'), reject_url: link('rejected'),
  // Requester-controlled text is HTML-escaped before it goes into an e-mail.
  html: `<p>${esc(r.requester)} requests <b>${esc(r.requested_scope)}</b> on
         <b>${esc(r.asset_type)} ${esc(r.asset_id)}${r.major_version ? ' v' + r.major_version : ''}</b>.</p>
         <p>Purpose: <b>${esc(r.purpose)}</b><br>Justification: ${esc(r.justification)}</p>
         <p>Approval route: ${esc(r.approval_route)} (stage ${STAGE} of ${plan.stages.length}, you approve as ${esc(stage.role)}).</p>
         <p><a href="${link('approved')}">Approve</a> &nbsp;|&nbsp; <a href="${link('rejected')}">Reject</a></p>
         <p style="color:#666">Link expires in ${plan.timeout_minutes} minutes. Request id ${esc(r.id)}.</p>`,
}}];
"""

VERIFY_JS = """
// [rule] Verify the clicked link. Timeout -> 'expired'. A bad signature, the wrong approver
// or the requester approving their own request -> 'invalid' (alerts ops, decides nothing).
const crypto = require('crypto');
const plan = $('Plan approval chain').first().json;
const stage = plan.stages[STAGE - 1];
const q = ($json.query) || {};
if (!q.decision) {
  return [{ json: { vote: 'expired', stage: STAGE, approver: null, route: 2,
    reason: `no decision within ${plan.timeout_minutes} minutes` } }];
}
const expected = crypto.createHmac('sha256', $env.NORDLYS_LINK_SIGNING_SECRET)
  .update([$execution.id, STAGE, q.approver, q.decision].join('|')).digest('hex');
const sigOk = typeof q.sig === 'string' && q.sig.length === expected.length &&
  crypto.timingSafeEqual(Buffer.from(q.sig), Buffer.from(expected));
let vote = q.decision === 'approved' ? 'approved' : q.decision === 'rejected' ? 'rejected' : 'invalid';
let reason = `${q.approver} ${q.decision} at stage ${STAGE}`;
if (!sigOk) { vote = 'invalid'; reason = 'approval link signature invalid'; }
else if (q.approver !== stage.approver) { vote = 'invalid'; reason = 'approver does not match this stage'; }
else if (q.approver === plan.request.requester_email) { vote = 'invalid'; reason = 'requester cannot approve own request'; }
const last = STAGE === plan.stages.length;
// Routing for the next Switch: 0 = next stage, 1 = final approve, 2 = final reject/expire, 3 = invalid.
const route = vote === 'approved' ? (last ? 1 : 0) : (vote === 'invalid' ? 3 : 2);
return [{ json: { vote, stage: STAGE, approver: vote === 'invalid' ? null : q.approver, reason, route } }];
"""


# --------------------------------------------------------------------------- workflow A


def access_request_workflow() -> JSON:
    f = Flow("A · Access request with human approval", "access-request-approval")
    X = 0

    def p(col: int, row: int = 0) -> tuple[int, int]:
        return (X + col * 260, row * 180)

    hook = f.node(
        "Access request created",
        "n8n-nodes-base.webhook",
        2.1,
        p(0),
        {
            "httpMethod": "POST",
            "path": "access-request-created",
            "responseMode": "responseNode",
            "options": {"rawBody": True},
        },
        webhookId=_id(f.slug, "webhook"),
        notes="[trigger] Signed event from the catalog after request_access",
        notesInFlow=True,
    )
    verify = f.code(
        "Verify signature & payload",
        p(1),
        """
// [rule] Deterministic checks before anything else happens:
//  - HMAC-SHA256 over "<timestamp>.<raw body>" with the shared webhook secret
//  - timestamp no older than 5 minutes (replay protection)
//  - payload shape: a pending access request with at least one approver
const crypto = require('crypto');
const item = $input.first();
const h = item.json.headers || {};
const ts = String(h['x-nordlys-timestamp'] || '');
const sig = String(h['x-nordlys-signature'] || '');
let raw;
if (item.binary && item.binary.data) {
  raw = await this.helpers.getBinaryDataBuffer(0, 'data');
} else {
  raw = Buffer.from(JSON.stringify(item.json.body));
}
const expected = 'sha256=' + crypto.createHmac('sha256', $env.NORDLYS_WEBHOOK_SECRET)
  .update(Buffer.concat([Buffer.from(ts + '.'), raw])).digest('hex');
const fresh = /^\\d+$/.test(ts) && Math.abs(Date.now() / 1000 - Number(ts)) <= 300;
const sigOk = sig.length === expected.length && crypto.timingSafeEqual(Buffer.from(sig), Buffer.from(expected));
const body = item.json.body || {};
const r = body.access_request || {};
const shapeOk = body.event === 'access_request.created' && /^[0-9a-f-]{36}$/.test(r.id || '')
  && r.status === 'pending_approval' && Array.isArray(r.approvers) && r.approvers.length > 0;
const valid = sigOk && fresh && shapeOk;
const reason = !sigOk ? 'bad signature' : !fresh ? 'stale or missing timestamp' : !shapeOk ? 'unexpected payload' : 'ok';
return [{ json: { valid, reason, request_id: body.request_id || null, request: valid ? r : null } }];
""",
        "[rule] HMAC + replay window + schema",
    )
    valid = f.if_true("Valid event?", p(2), "={{ $json.valid }}", "[rule]")
    reject401 = f.node(
        "Reject (401)",
        "n8n-nodes-base.respondToWebhook",
        1.5,
        p(3, 1),
        {"respondWith": "json", "responseBody": '={{ { "error": $json.reason } }}', "options": {"responseCode": 401}},
    )
    accept202 = f.node(
        "Accept (202)",
        "n8n-nodes-base.respondToWebhook",
        1.5,
        p(3),
        {
            "respondWith": "json",
            "responseBody": '={{ { "accepted": true, "access_request_id": $json.request.id } }}',
            "options": {"responseCode": 202},
        },
        notes="Answer the catalog immediately; review continues asynchronously",
        notesInFlow=True,
    )
    token1 = f.http(
        "Service token",
        p(4),
        method="POST",
        url="={{ $env.NORDLYS_TOKEN_URL }}",
        form=[
            ("grant_type", "client_credentials"),
            ("client_id", "={{ $env.NORDLYS_N8N_CLIENT_ID }}"),
            ("client_secret", "={{ $env.NORDLYS_N8N_CLIENT_SECRET }}"),
        ],
        note="[system] OAuth client credentials (n8n's own identity)",
    )
    reread = f.http(
        "Re-read request",
        p(5),
        method="GET",
        url="={{ $env.NORDLYS_CATALOG_URL }}/v1/access-requests/{{ $('Verify signature & payload').first().json.request.id }}",
        headers=[("Authorization", "=Bearer {{ $('Service token').first().json.access_token }}")],
        note="[rule] Source of truth is the catalog, not the event payload",
    )
    plan = f.code(
        "Plan approval chain",
        p(6),
        """
// [rule] Map the approval route to an ordered chain of human approvers (deterministic).
// The directory below stands in for an HR/IAM lookup (Entra ID groups in production).
const DIRECTORY = {
  'role:data-protection-officer': ['dpo@nordlys.example'],
  'role:information-security': ['infosec@nordlys.example'],
  'team:partner-integrations': ['bob@nordlys.example'],
};
const DEFAULT_TEAM_APPROVER = 'bob@nordlys.example';  // team leads; one per team in a real directory
const r = $json;
const requesterEmail = r.requester.includes('@') ? r.requester : `${r.requester}@nordlys.example`;
const stages = r.approvers.map((role) => {
  const pool = DIRECTORY[role] || (role.startsWith('team:') ? [DEFAULT_TEAM_APPROVER] : []);
  // Four-eyes: never route a request to its own requester.
  const approver = pool.find((a) => a !== requesterEmail);
  return { role, approver: approver || null };
});
const unroutable = stages.filter((s) => !s.approver).map((s) => s.role);
const timeout_minutes = Number($env.NORDLYS_APPROVAL_TIMEOUT_MINUTES || 1440);
const still_pending = r.status === 'pending_approval';
// Routing: 0 = start approvals, 1 = nothing to do (already decided), 2 = cannot route -> ops.
const route = !still_pending ? 1 : unroutable.length ? 2 : 0;
return [{ json: { route, still_pending, unroutable, stages, timeout_minutes,
  request: { ...r, requester_email: requesterEmail } } }];
""",
        "[rule] Route -> ordered approvers; four-eyes",
    )
    plan_switch = f.switch("Route", p(7), "={{ $json.route }}", 3, "[rule] 0 approve · 1 done · 2 unroutable")
    done = f.node(
        "Already decided",
        "n8n-nodes-base.noOp",
        1,
        p(8, 1),
        {},
        notes="Idempotent: a redelivered event changes nothing",
        notesInFlow=True,
    )

    def stage(n: int, col: int) -> tuple[str, str]:
        sign = f.code(
            f"Sign links (stage {n})", p(col, 0), SIGN_JS.replace("STAGE", str(n)), "[rule] HMAC-signed links"
        )
        mail = f.email(
            f"E-mail approver (stage {n})",
            p(col + 1, 0),
            to="={{ $json.approver }}",
            subject="=Access request: {{ $('Plan approval chain').first().json.request.requested_scope }} "
            "for {{ $('Plan approval chain').first().json.request.requester }}",
            html="={{ $json.html }}",
            note="[human] Approve / Reject links",
        )
        wait = f.node(
            f"Wait for decision (stage {n})",
            "n8n-nodes-base.wait",
            1.1,
            p(col + 2, 0),
            {
                "resume": "webhook",
                "httpMethod": "GET",
                "responseMode": "onReceived",
                "limitWaitTime": True,
                "limitType": "afterTimeInterval",
                "resumeAmount": "={{ $('Plan approval chain').first().json.timeout_minutes }}",
                "resumeUnit": "minutes",
                "options": {"responseData": "Thank you - your decision was received. You can close this tab."},
            },
            webhookId=_id(f.slug, f"wait-{n}"),
            notes="[human] Pauses until a link is clicked or the timeout",
            notesInFlow=True,
        )
        check = f.code(
            f"Verify vote (stage {n})",
            p(col + 3, 0),
            VERIFY_JS.replace("STAGE", str(n)),
            "[rule] signature, approver, four-eyes, timeout",
        )
        sw = f.switch(
            f"Stage {n} outcome",
            p(col + 4, 0),
            "={{ $json.route }}",
            4,
            "[rule] 0 next · 1 approve · 2 reject/expire · 3 invalid",
        )
        f.link(sign, mail)
        f.link(mail, wait)
        f.link(wait, check)
        f.link(check, sw)
        return sign, sw

    s1_in, s1_out = stage(1, 8)
    s2_in, s2_out = stage(2, 13)

    final = f.code(
        "Final decision",
        p(18, 1),
        """
// [rule] Combine the stage votes into one decision. Only humans approve: 'approved' requires
// every stage to have been approved by a verified approver link.
const plan = $('Plan approval chain').first().json;
const votes = [];
for (const n of [1, 2]) {
  try { votes.push($(`Verify vote (stage ${n})`).first().json); } catch (e) { /* stage not run */ }
}
const last = votes[votes.length - 1];
const approved = votes.length === plan.stages.length && votes.every((v) => v.vote === 'approved');
const expired = last && last.vote === 'expired';
const decided_by = approved || !expired ? last.approver : 'workflow-timeout@nordlys.example';
const note = approved
  ? `Approved by ${votes.map((v) => v.approver).join(' and ')} via n8n workflow A`
  : expired ? `Expired: ${last.reason}` : `Rejected by ${last.approver} at stage ${last.stage}`;
return [{ json: { decision: approved ? 'approved' : 'rejected', decided_by, note, expired: !!expired,
  request: plan.request, votes } }];
""",
        "[rule] all stages approved -> approved",
    )
    token2 = f.http(
        "Fresh service token",
        p(19, 1),
        method="POST",
        url="={{ $env.NORDLYS_TOKEN_URL }}",
        form=[
            ("grant_type", "client_credentials"),
            ("client_id", "={{ $env.NORDLYS_N8N_CLIENT_ID }}"),
            ("client_secret", "={{ $env.NORDLYS_N8N_CLIENT_SECRET }}"),
        ],
        note="[system] The first token expired while we waited for humans",
    )
    record = f.http(
        "Record decision in catalog",
        p(20, 1),
        method="POST",
        url="={{ $env.NORDLYS_CATALOG_URL }}/v1/access-requests/{{ $('Final decision').first().json.request.id }}/decision",
        headers=[
            ("Authorization", "=Bearer {{ $json.access_token }}"),
            ("X-On-Behalf-Of-Subject", "={{ $('Final decision').first().json.decided_by }}"),
            ("X-On-Behalf-Of-Scopes", "access.approve"),
        ],
        json_body='={{ { "decided_by": $("Final decision").first().json.decided_by, '
        '"decision": $("Final decision").first().json.decision, "note": $("Final decision").first().json.note } }}',
        note="[system] Catalog enforces four-eyes + audits the decision",
    )
    approved = f.if_true(
        "Approved?", p(21, 1), '={{ $("Final decision").first().json.decision === "approved" }}', "[rule]"
    )
    find_user = f.http(
        "Find requester in IdP",
        p(22, 0),
        method="GET",
        url="={{ $env.NORDLYS_KEYCLOAK_ADMIN_URL }}/users?exact=true&username={{ encodeURIComponent($('Final decision').first().json.request.requester) }}",
        headers=[("Authorization", "=Bearer {{ $('Fresh service token').first().json.access_token }}")],
        note="[system] Keycloak Admin API (Entra: Microsoft Graph)",
    )
    ensure_role = f.http(
        "Ensure role exists",
        p(23, 0),
        method="POST",
        url="={{ $env.NORDLYS_KEYCLOAK_ADMIN_URL }}/roles",
        headers=[("Authorization", "=Bearer {{ $('Fresh service token').first().json.access_token }}")],
        json_body='={{ { "name": $("Final decision").first().json.request.requested_scope, '
        '"description": "Granted via approved access requests" } }}',
        never_error=True,
        note="[system] 201 created or 409 already exists - both fine",
    )
    get_role = f.http(
        "Get role",
        p(24, 0),
        method="GET",
        url="={{ $env.NORDLYS_KEYCLOAK_ADMIN_URL }}/roles/{{ encodeURIComponent($('Final decision').first().json.request.requested_scope) }}",
        headers=[("Authorization", "=Bearer {{ $('Fresh service token').first().json.access_token }}")],
        note="[system]",
    )
    grant = f.http(
        "Grant role to requester",
        p(25, 0),
        method="POST",
        url="={{ $env.NORDLYS_KEYCLOAK_ADMIN_URL }}/users/{{ $('Find requester in IdP').first().json.id }}/role-mappings/realm",
        headers=[("Authorization", "=Bearer {{ $('Fresh service token').first().json.access_token }}")],
        json_body='={{ [ { "id": $("Get role").first().json.id, "name": $("Get role").first().json.name } ] }}',
        note="[system] The actual grant - only after human approval",
    )
    audit_grant = f.http(
        "Audit: access granted",
        p(26, 0),
        method="POST",
        url="={{ $env.NORDLYS_CATALOG_URL }}/v1/audit-events",
        headers=[("Authorization", "=Bearer {{ $('Fresh service token').first().json.access_token }}")],
        json_body='={{ { "actor": $("Final decision").first().json.decided_by, "action": "access_grant.applied", '
        '"resource": "idp-role:" + $("Final decision").first().json.request.requested_scope, "decision": "approved", '
        '"reason": "role assigned to " + $("Final decision").first().json.request.requester, '
        '"request_id": $("Final decision").first().json.request.id, "details": { "idp": "keycloak", "workflow_execution": $execution.id } } }}',
        note="[system] Append-only audit log",
    )
    notify = f.email(
        "Notify requester",
        p(27, 1),
        to="={{ $('Final decision').first().json.request.requester_email }}",
        subject="=Your access request for {{ $('Final decision').first().json.request.requested_scope }} "
        "was {{ $('Final decision').first().json.decision }}",
        html="=<p>Your request for <b>{{ $('Final decision').first().json.request.requested_scope }}</b> "
        "({{ $('Final decision').first().json.request.asset_id }}) was <b>{{ $('Final decision').first().json.decision }}</b>.</p>"
        "<p>{{ $('Final decision').first().json.note }}</p>"
        "<p>{{ $('Final decision').first().json.decision === 'approved' ? 'Request a new token to use the new access.' : '' }}</p>",
        note="[system] Close the loop with the requester",
    )

    # error branch
    err = f.code(
        "Describe failure",
        p(22, 3),
        """
// [rule] Any failed call ends here: describe it for humans and for the audit log.
// The access request is untouched (still pending or already decided), so nothing is lost.
const input = $input.first().json;
let request_id = null;
try { request_id = $('Verify signature & payload').first().json.request.id; } catch (e) {}
const what = input.unroutable ? `no approver for ${input.unroutable.join(', ')}`
  : input.vote === 'invalid' ? input.reason
  : (input.error && (input.error.message || JSON.stringify(input.error))) || 'unknown error';
return [{ json: { request_id, what: String(what).slice(0, 500), execution: $execution.id } }];
""",
        "[rule] error branch",
    )
    ops = f.email(
        "Alert platform ops",
        p(23, 3),
        to="={{ $env.NORDLYS_OPS_EMAIL }}",
        subject="=Access workflow needs attention: {{ $json.request_id }}",
        html="=<p>Workflow A could not complete for access request {{ $json.request_id }}.</p>"
        "<p>{{ $json.what }}</p><p>n8n execution {{ $json.execution }}. The request has not been decided automatically.</p>",
        note="[human] Ops takes over",
    )

    f.link(hook, verify)
    f.link(verify, valid)
    f.link(valid, accept202, 0)
    f.link(valid, reject401, 1)
    f.link(accept202, token1)
    f.link(token1, reread, 0)
    f.link(token1, err, 1)
    f.link(reread, plan, 0)
    f.link(reread, err, 1)
    f.link(plan, plan_switch)
    f.link(plan_switch, s1_in, 0)
    f.link(plan_switch, done, 1)
    f.link(plan_switch, err, 2)
    f.link(s1_out, s2_in, 0)
    f.link(s1_out, final, 1)
    f.link(s1_out, final, 2)
    f.link(s1_out, err, 3)
    f.link(s2_out, final, 1)
    f.link(s2_out, final, 2)
    f.link(s2_out, err, 3)
    f.link(final, token2)
    f.link(token2, record, 0)
    f.link(token2, err, 1)
    f.link(record, approved, 0)
    f.link(record, err, 1)
    f.link(approved, find_user, 0)
    f.link(approved, notify, 1)
    f.link(find_user, ensure_role, 0)
    f.link(find_user, err, 1)
    f.link(ensure_role, get_role, 0)
    f.link(ensure_role, err, 1)
    f.link(get_role, grant, 0)
    f.link(get_role, err, 1)
    f.link(grant, audit_grant, 0)
    f.link(grant, err, 1)
    f.link(audit_grant, notify, 0)
    f.link(audit_grant, err, 1)
    f.link(err, ops)
    f.layout(
        {
            # lane 0: intake (rules)
            "Access request created": (0, 0),
            "Verify signature & payload": (1, 0),
            "Valid event?": (2, 0),
            "Accept (202)": (3, 0),
            "Reject (401)": (3, 1),
            "Service token": (4, 0),
            "Re-read request": (5, 0),
            "Plan approval chain": (6, 0),
            "Route": (7, 0),
            "Already decided": (8, 0),
            # lane 2: stage 1, lane 3: stage 2 (humans)
            **{
                f"{name} (stage {s})": (c, 1 + s)
                for s in (1, 2)
                for c, name in enumerate(["Sign links", "E-mail approver", "Wait for decision", "Verify vote"], start=3)
            },
            "Stage 1 outcome": (7, 2),
            "Stage 2 outcome": (7, 3),
            # lane 4: decision and grant (system)
            "Final decision": (0, 4),
            "Fresh service token": (1, 4),
            "Record decision in catalog": (2, 4),
            "Approved?": (3, 4),
            "Find requester in IdP": (4, 4),
            "Ensure role exists": (5, 4),
            "Get role": (6, 4),
            "Grant role to requester": (7, 4),
            "Audit: access granted": (8, 4),
            "Notify requester": (9, 4),
            # lane 5: error branch
            "Describe failure": (8, 5),
            "Alert platform ops": (9, 5),
        }
    )
    return f.to_json({"errorWorkflow": error_workflow_id()})


# --------------------------------------------------------------------------- workflow B

REVIEW_SIGN_JS = """
// [rule] Signed Accept / Reject links for the reviewer (same scheme as workflow A).
const crypto = require('crypto');
const g = $('Decision gate').first().json;
const reviewer = $env.NORDLYS_REVIEWER_EMAIL;
// The chosen domain is part of the signature, so it cannot be edited in the link either.
const sign = (d, dom) => crypto.createHmac('sha256', $env.NORDLYS_LINK_SIGNING_SECRET)
  .update([$execution.id, 'review', reviewer, d, dom].join('|')).digest('hex');
const base = $execution.resumeUrl;
const link = (d, dom = '') => `${base}${base.includes('?') ? '&' : '?'}reviewer=${encodeURIComponent(reviewer)}&decision=${d}&domain=${dom}&sig=${sign(d, dom)}`;
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const c = g.check, ai = g.ai;
const aiBlock = ai.available
  ? `<p><b>AI suggestion</b> (${esc(ai.model)}, advisory only): domain <b>${esc(ai.classification.domain)}</b>
     (${Math.round(ai.classification.domain_confidence * 100)}%), owner <b>${esc(ai.classification.owner_team)}</b>
     (${Math.round(ai.classification.owner_confidence * 100)}%), doc quality ${ai.classification.doc_quality}/5.<br>
     ${esc(ai.classification.summary)}<br>Gaps: ${esc((ai.classification.doc_gaps || []).join('; '))}</p>`
  : `<p><b>AI suggestion unavailable</b>: ${esc(ai.reason)}. Review manually.</p>`;
return [{ json: { reviewer, html: `
  <p><b>${esc(c.title)}</b> (${esc(c.api_id)} v${c.major_version}) submitted by ${esc(g.submitted_by)}.</p>
  <p>Proposed: domain <b>${esc(g.proposal.domain)}</b>, owner <b>${esc(g.proposal.owner_team)}</b>.</p>
  <p><b>Why you are asked:</b></p><ul>${g.review_reasons.map((r) => `<li>${esc(r)}</li>`).join('')}</ul>
  <p><b>Checks (deterministic):</b> description coverage ${Math.round(c.description_coverage * 100)}%,
     content warnings: ${esc(c.content_warnings.join(', ') || 'none')},
     possible duplicates (>= 30% shared endpoints): ${esc(c.duplicates.filter((d) => d.endpoint_overlap >= 0.3).map((d) => `${d.api_id} v${d.major_version} (${Math.round(d.endpoint_overlap * 100)}%)`).join(', ') || 'none')};
     closest existing API by search: ${esc((c.duplicates.find((d) => d.search_rank === 1) || {}).api_id || 'n/a')}.</p>
  ${aiBlock}
  ${g.proposal.domain !== 'unknown'
    ? `<p><a href="${link('accepted', g.proposal.domain)}">Accept as proposed</a> &nbsp;|&nbsp; <a href="${link('rejected')}">Reject</a></p>`
    : `<p>No domain could be determined. Accept into domain:</p><p>${c.known_domains.map((d) =>
        `<a href="${link('accepted', d)}">${esc(d)}</a>`).join(' &nbsp;|&nbsp; ')}</p><p><a href="${link('rejected')}">Reject</a></p>`}` } }];
"""

REVIEW_VERIFY_JS = """
// [rule] Verify the reviewer's link. Timeout -> rejected (expired). Bad signature -> ops.
const crypto = require('crypto');
const q = ($json.query) || {};
if (!q.decision) return [{ json: { route: 1, outcome: 'expired', reviewer: null } }];
const dom = q.domain || '';
const expected = crypto.createHmac('sha256', $env.NORDLYS_LINK_SIGNING_SECRET)
  .update([$execution.id, 'review', q.reviewer, q.decision, dom].join('|')).digest('hex');
const known = $('Deterministic checks (catalog)').first().json.known_domains;
const ok = typeof q.sig === 'string' && q.sig.length === expected.length &&
  crypto.timingSafeEqual(Buffer.from(q.sig), Buffer.from(expected)) && q.reviewer === $env.NORDLYS_REVIEWER_EMAIL
  && (q.decision !== 'accepted' || known.includes(dom));
if (!ok) return [{ json: { route: 2, outcome: 'invalid', reviewer: null, vote: 'invalid', reason: 'review link signature invalid' } }];
// 0 = publish, 1 = reject, 2 = invalid
return [{ json: { route: q.decision === 'accepted' ? 0 : 1, outcome: q.decision, reviewer: q.reviewer, domain: dom || null } }];
"""


def registration_workflow() -> JSON:
    f = Flow("B · New API registration (agent + human review)", "api-registration")

    def p(col: int, row: int = 0) -> tuple[int, int]:
        return (col * 260, row * 180)

    hook = f.node(
        "API registration submitted",
        "n8n-nodes-base.webhook",
        2.1,
        p(0),
        {"httpMethod": "POST", "path": "api-registration", "responseMode": "responseNode", "options": {}},
        webhookId=_id(f.slug, "webhook"),
        notes="[trigger] Portal or CI pipeline submits an OpenAPI spec",
        notesInFlow=True,
    )
    intake = f.code(
        "Authenticate & read submission",
        p(1),
        """
// [rule] Shared API key (constant-time compare), required fields, size limit.
const crypto = require('crypto');
const item = $input.first().json;
const key = String((item.headers || {})['x-api-key'] || '');
const expected = String($env.NORDLYS_REGISTRATION_API_KEY || '');
const authOk = key.length === expected.length && expected.length > 0 &&
  crypto.timingSafeEqual(Buffer.from(key), Buffer.from(expected));
const b = item.body || {};
const okShape = typeof b.spec === 'string' && b.spec.length > 10 && b.spec.length <= 512 * 1024
  && typeof b.submitted_by === 'string' && /^[^@\\s]+@[^@\\s]+$/.test(b.submitted_by);
return [{ json: { ok: authOk && okShape, status: !authOk ? 401 : 400,
  reason: !authOk ? 'invalid API key' : !okShape ? 'need spec (string) and submitted_by (e-mail)' : 'ok',
  spec: okShape ? b.spec : null, submitted_by: okShape ? b.submitted_by : null } }];
""",
        "[rule] API key, schema, size",
    )
    ok = f.if_true("Accepted?", p(2), "={{ $json.ok }}", "[rule]")
    refuse = f.node(
        "Refuse submission",
        "n8n-nodes-base.respondToWebhook",
        1.5,
        p(3, 1),
        {
            "respondWith": "json",
            "responseBody": '={{ { "error": $json.reason } }}',
            "options": {"responseCode": "={{ $json.status }}"},
        },
    )
    ack = f.node(
        "Acknowledge (202)",
        "n8n-nodes-base.respondToWebhook",
        1.5,
        p(3),
        {
            "respondWith": "json",
            "responseBody": '={{ { "accepted": true, "tracking_id": $execution.id } }}',
            "options": {"responseCode": 202},
        },
    )
    token = f.http(
        "Service token",
        p(4),
        method="POST",
        url="={{ $env.NORDLYS_TOKEN_URL }}",
        form=[
            ("grant_type", "client_credentials"),
            ("client_id", "={{ $env.NORDLYS_N8N_CLIENT_ID }}"),
            ("client_secret", "={{ $env.NORDLYS_N8N_CLIENT_SECRET }}"),
        ],
        note="[system]",
    )
    checks = f.http(
        "Deterministic checks (catalog)",
        p(5),
        method="POST",
        url="={{ $env.NORDLYS_CATALOG_URL }}/v1/registrations/validate",
        headers=[("Authorization", "=Bearer {{ $('Service token').first().json.access_token }}")],
        json_body='={{ { "spec": $("Authenticate & read submission").first().json.spec } }}',
        note="[rule] OpenAPI 3.1, metadata, coverage, injection scan, duplicates, version",
    )
    valid = f.if_true("Spec valid?", p(6), "={{ $json.valid }}", "[rule]")
    ai = f.http(
        "AI classification (agent)",
        p(7),
        method="POST",
        url="={{ $env.NORDLYS_AGENT_URL }}/v1/classify-registration",
        headers=[("Authorization", "=Bearer {{ $('Service token').first().json.access_token }}")],
        json_body='={{ { "check": $("Deterministic checks (catalog)").first().json } }}',
        note="[AI] domain, owner, doc critique + confidence (advisory)",
    )
    gate = f.code(
        "Decision gate",
        p(8),
        """
// [rule] The gate is deterministic. The AI suggests; rules decide whether a human must look.
// Auto-accept only when every signal agrees and nothing is risky.
const c = $('Deterministic checks (catalog)').first().json;
const ai = $json;
const sub = $('Authenticate & read submission').first().json;
const cls = ai.available ? ai.classification : null;
const reasons = [];
if (c.version_exists) {
  return [{ json: { route: 2, reject_reason: `${c.api_id} v${c.major_version} already exists - bump the major version`,
    check: c, ai, submitted_by: sub.submitted_by } }];
}
if (!cls) reasons.push(`AI step unavailable (${ai.reason || 'no model'})`);
if (cls && cls.domain_confidence < 0.8) reasons.push(`AI domain confidence ${Math.round(cls.domain_confidence * 100)}% < 80%`);
if (cls && cls.owner_confidence < 0.8) reasons.push(`AI owner confidence ${Math.round(cls.owner_confidence * 100)}% < 80%`);
if (cls && c.declared_domain && cls.domain !== c.declared_domain) reasons.push(`AI says ${cls.domain}, spec declares ${c.declared_domain}`);
if (cls && cls.owner_team === 'unknown' && !c.declared_owner_team) reasons.push('no owner team identified');
if (cls && cls.doc_quality <= 2) reasons.push(`AI rates documentation ${cls.doc_quality}/5`);
if (cls && cls.notes) reasons.push(`AI notes: ${cls.notes}`);
if (c.content_warnings.length) reasons.push(`content warnings (possible prompt injection): ${c.content_warnings.join(', ')}`);
if (c.description_coverage < 0.8) reasons.push(`only ${Math.round(c.description_coverage * 100)}% of operations documented`);
for (const d of c.duplicates) {
  if (d.endpoint_overlap >= 0.5) reasons.push(`possible duplicate of ${d.api_id} v${d.major_version} (${Math.round(d.endpoint_overlap * 100)}% endpoint overlap)`);
}
const proposal = {
  domain: c.declared_domain || (cls ? cls.domain : 'unknown'),
  owner_team: c.declared_owner_team || (cls && cls.owner_team !== 'unknown' ? cls.owner_team : 'api-governance'),
};
if (proposal.domain === 'unknown') reasons.push('no domain identified');
// 0 = auto-accept, 1 = human review, 2 = reject
return [{ json: { route: reasons.length ? 1 : 0, review_reasons: reasons, proposal, check: c, ai,
  submitted_by: sub.submitted_by } }];
""",
        "[rule] combine AI + rules -> auto / review / reject",
    )
    route = f.switch("Route", p(9), "={{ $json.route }}", 3, "[rule] 0 auto · 1 review · 2 reject")
    sign = f.code("Prepare review", p(10, 1), REVIEW_SIGN_JS, "[rule] signed review links")
    mail = f.email(
        "E-mail reviewer",
        p(11, 1),
        to="={{ $json.reviewer }}",
        subject="=Review API registration: {{ $('Decision gate').first().json.check.title }}",
        html="={{ $json.html }}",
        note="[human] API governance reviews",
    )
    wait = f.node(
        "Wait for review",
        "n8n-nodes-base.wait",
        1.1,
        p(12, 1),
        {
            "resume": "webhook",
            "httpMethod": "GET",
            "responseMode": "onReceived",
            "limitWaitTime": True,
            "limitType": "afterTimeInterval",
            "resumeAmount": "={{ Number($env.NORDLYS_REVIEW_TIMEOUT_MINUTES || 4320) }}",
            "resumeUnit": "minutes",
            "options": {"responseData": "Thank you - your review was received."},
        },
        webhookId=_id(f.slug, "wait"),
        notes="[human] Pauses until a link is clicked or the timeout",
        notesInFlow=True,
    )
    verify = f.code("Verify review", p(13, 1), REVIEW_VERIFY_JS, "[rule] signature + timeout")
    review_route = f.switch(
        "Review outcome", p(14, 1), "={{ $json.route }}", 3, "[rule] 0 publish · 1 reject · 2 invalid"
    )
    fresh = f.http(
        "Fresh service token",
        p(15, 0),
        method="POST",
        url="={{ $env.NORDLYS_TOKEN_URL }}",
        form=[
            ("grant_type", "client_credentials"),
            ("client_id", "={{ $env.NORDLYS_N8N_CLIENT_ID }}"),
            ("client_secret", "={{ $env.NORDLYS_N8N_CLIENT_SECRET }}"),
        ],
        note="[system] tokens expire while humans review",
    )
    publish = f.http(
        "Publish to catalog",
        p(16, 0),
        method="POST",
        url="={{ $env.NORDLYS_CATALOG_URL }}/v1/registrations",
        headers=[("Authorization", "=Bearer {{ $json.access_token }}")],
        json_body='={{ { "spec": $("Authenticate & read submission").first().json.spec, '
        '"domain": $("Decision gate").first().json.route === 0 ? $("Decision gate").first().json.proposal.domain : $("Verify review").first().json.domain, '
        '"owner_team": $("Decision gate").first().json.proposal.owner_team, '
        '"submitted_by": $("Decision gate").first().json.submitted_by, '
        '"decision": $("Decision gate").first().json.route === 0 ? "auto_accepted" : "human_approved", '
        '"reviewed_by": $("Decision gate").first().json.route === 0 ? null : $("Verify review").first().json.reviewer } }}',
        note="[system] write spec + ingest (embeddings, search) + audit",
    )
    published = f.email(
        "Tell submitter: published",
        p(17, 0),
        to="={{ $('Decision gate').first().json.submitted_by }}",
        subject="=Your API {{ $('Publish to catalog').first().json.api_id }} v{{ $('Publish to catalog').first().json.major_version }} is in the catalog",
        html="=<p>Published as <b>{{ $('Publish to catalog').first().json.api_id }}</b> "
        "v{{ $('Publish to catalog').first().json.major_version }} in domain "
        "{{ $('Decision gate').first().json.proposal.domain }}, owned by {{ $('Decision gate').first().json.proposal.owner_team }}.</p>"
        "<p>Decision: {{ $('Decision gate').first().json.route === 0 ? 'automatically accepted (all checks passed)' : 'approved by ' + $('Verify review').first().json.reviewer }}.</p>",
        note="[system]",
    )
    rejected = f.email(
        "Tell submitter: not published",
        p(16, 2),
        to="={{ $('Decision gate').first().json.submitted_by }}",
        subject="=Your API registration was not published",
        html="=<p>Your registration of <b>{{ $('Decision gate').first().json.check.title }}</b> was not published.</p>"
        "<p>{{ $('Decision gate').first().json.reject_reason || ($json.outcome === 'expired' ? 'No review decision within the review period.' : 'Rejected by the API governance reviewer.') }}</p>",
        note="[system]",
    )
    invalid = f.email(
        "Tell submitter: invalid spec",
        p(7, 2),
        to="={{ $('Authenticate & read submission').first().json.submitted_by }}",
        subject="=Your OpenAPI spec could not be registered",
        html="=<p>The specification failed validation:</p><ul>{{ $json.errors.map(e => '<li>' + e.replace(/</g, '&lt;') + '</li>').join('') }}</ul>",
        note="[rule] fail fast, no AI call needed",
    )
    err = f.code(
        "Describe failure",
        p(17, 3),
        """
// [rule] Any failed call ends here. Nothing is published by a failed run.
const input = $input.first().json;
const what = input.vote === 'invalid' ? input.reason
  : (input.error && (input.error.message || JSON.stringify(input.error))) || 'unknown error';
return [{ json: { what: String(what).slice(0, 500), execution: $execution.id } }];
""",
        "[rule] error branch",
    )
    ops = f.email(
        "Alert platform ops",
        p(18, 3),
        to="={{ $env.NORDLYS_OPS_EMAIL }}",
        subject="=API registration workflow needs attention ({{ $json.execution }})",
        html="=<p>{{ $json.what }}</p><p>n8n execution {{ $json.execution }}. Nothing was published.</p>",
        note="[human]",
    )

    f.link(hook, intake)
    f.link(intake, ok)
    f.link(ok, ack, 0)
    f.link(ok, refuse, 1)
    f.link(ack, token)
    f.link(token, checks, 0)
    f.link(token, err, 1)
    f.link(checks, valid, 0)
    f.link(checks, err, 1)
    f.link(valid, ai, 0)
    f.link(valid, invalid, 1)
    f.link(ai, gate, 0)
    f.link(ai, err, 1)
    f.link(gate, route)
    f.link(route, fresh, 0)
    f.link(route, sign, 1)
    f.link(route, rejected, 2)
    f.link(sign, mail)
    f.link(mail, wait)
    f.link(wait, verify)
    f.link(verify, review_route)
    f.link(review_route, fresh, 0)
    f.link(review_route, rejected, 1)
    f.link(review_route, err, 2)
    f.link(fresh, publish, 0)
    f.link(fresh, err, 1)
    f.link(publish, published, 0)
    f.link(publish, err, 1)
    f.link(err, ops)
    f.layout(
        {
            "API registration submitted": (0, 0),
            "Authenticate & read submission": (1, 0),
            "Accepted?": (2, 0),
            "Acknowledge (202)": (3, 0),
            "Refuse submission": (3, 1),
            "Service token": (4, 0),
            "Deterministic checks (catalog)": (5, 0),
            "Spec valid?": (6, 0),
            "Tell submitter: invalid spec": (7, 1),
            "AI classification (agent)": (7, 0),
            "Decision gate": (8, 0),
            "Route": (9, 0),
            "Prepare review": (3, 2),
            "E-mail reviewer": (4, 2),
            "Wait for review": (5, 2),
            "Verify review": (6, 2),
            "Review outcome": (7, 2),
            "Tell submitter: not published": (9, 2),
            "Fresh service token": (7, 3),
            "Publish to catalog": (8, 3),
            "Tell submitter: published": (9, 3),
            "Describe failure": (8, 4),
            "Alert platform ops": (9, 4),
        }
    )
    return f.to_json({"errorWorkflow": error_workflow_id()})


# --------------------------------------------------------------------------- error handler

ERROR_WORKFLOW_SLUG = "error-handler"


def error_workflow_id() -> str:
    return str(uuid.uuid5(NS, ERROR_WORKFLOW_SLUG)).replace("-", "")[:16]


def error_handler_workflow() -> JSON:
    """Catches any failure the workflows did not route themselves (n8n 'Error Trigger')."""
    f = Flow("Z · Error handler (alerts platform ops)", ERROR_WORKFLOW_SLUG)
    trig = f.node(
        "On workflow error",
        "n8n-nodes-base.errorTrigger",
        1,
        (0, 0),
        {},
        notes="[trigger] Any unhandled failure in workflows A or B",
        notesInFlow=True,
    )
    mail = f.email(
        "Alert platform ops",
        (260, 0),
        to="={{ $env.NORDLYS_OPS_EMAIL }}",
        subject="=n8n workflow failed: {{ $json.workflow.name }}",
        html="=<p>Workflow <b>{{ $json.workflow.name }}</b> failed in node "
        "<b>{{ $json.execution.lastNodeExecuted }}</b>.</p><p>{{ $json.execution.error.message }}</p>"
        "<p>Execution {{ $json.execution.id }}: {{ $json.execution.url }}</p>"
        "<p>No access was granted by a failed run: grants happen only after a recorded human approval.</p>",
        note="[human] Ops investigates",
    )
    f.link(trig, mail)
    wf = f.to_json()
    wf["id"] = error_workflow_id()
    return wf


# --------------------------------------------------------------------------- main

WORKFLOWS = {
    "access-request-approval.json": access_request_workflow,
    "api-registration.json": registration_workflow,
    "error-handler.json": error_handler_workflow,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    drift = []
    for name, build in WORKFLOWS.items():
        text = json.dumps(build(), indent=2, ensure_ascii=False) + "\n"
        path = OUT / name
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                drift.append(name)
        else:
            path.write_text(text, encoding="utf-8")
    if drift:
        print(f"n8n workflows drifted from the generator: {drift}", file=sys.stderr)
        return 1
    print(("OK: " if args.check else "Wrote ") + ", ".join(WORKFLOWS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
