# ADR 0003: Access is requested by tools and granted only by humans

**Status:** accepted

## Context

An AI assistant that can grant data access is a privilege-escalation path: a prompt
injection in an API description, or a persuasive user, could talk it into granting.
Insurance data includes health and fraud data under GDPR, where access must be purpose-
bound, approved and auditable.

## Decision

* The MCP tool `request_access` can only create a request in `pending_approval`. No code
  path in the MCP server, the catalog API or the agent grants access.
* The discovery agent is not even offered `request_access`; filing a request stays a
  deliberate user action.
* Approval runs in n8n Workflow A: policy check, e-mail to the named approvers with
  HMAC-signed approve/reject links, a timeout, and only then a role grant in the identity
  provider. The decision is written to the append-only audit log and the requester is
  notified.
* The database enforces what matters even if application code is bypassed: valid
  status transitions, one open request per requester/asset/purpose, and four-eyes (an
  approver is never the requester). The audit table rejects UPDATE and DELETE.
* The policy (which scope, which approvers, which purposes are prohibited) is code with
  tests (`access/policy.py`, `tests/test_access_policy.py`).

## Consequences

+ The worst outcome of a successful injection is a pending request a human will see.
+ Every grant has a human approver, a purpose and an audit trail.
- Access takes as long as the slowest approver. A timeout expires the request (rules may
  auto-deny, never auto-grant) and tells the requester; invalid or unroutable cases alert
  platform operations.
- n8n becomes part of the security boundary: its editor is network-restricted, links are
  signed and time-limited, and its service account can manage only realm roles.
