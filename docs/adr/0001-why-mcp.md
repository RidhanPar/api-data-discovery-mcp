# ADR 0001: Expose the catalog through MCP

**Status:** accepted

## Context

Developers and analysts ask discovery questions in many places: Claude Desktop, VS Code,
internal chat assistants, and soon autonomous agents. Each of those would otherwise need
its own integration with the catalog, its own auth handling and its own idea of what a
"citation" is. The catalog also has rules that must hold whoever asks: no sample values
from sensitive data, access only through human approval, audit of every call.

## Decision

Put a Model Context Protocol server in front of the catalog service, built on the
official Python SDK, with Streamable HTTP for remote clients and stdio for local use.
Expose eight narrow, typed tools (search, details, endpoint schema, version diff, data
product, check access, request access, deprecations), three resources and two prompts.
The MCP server is a thin, stateless layer; the catalog service stays the system of record
and is usable over plain HTTP (n8n uses it directly).

## Consequences

+ One integration serves every MCP-capable client; the discovery agent in this repo uses
  the same server as its **only** tool source, so the agent cannot do anything a user's
  own client could not.
+ Policy lives server-side, in one place: tool scopes, rate limits, audit, sample-data
  rules and "request, never grant" are enforced in the guard and the catalog, not in
  prompts.
+ Typed tool schemas reject bad arguments before any code runs (contract-tested).
- MCP is young: the SDK's v2 API changed during the build (FastMCP → MCPServer), and
  client support for OAuth discovery varies. Pinned versions and contract tests contain
  this.
- An extra hop. Measured MCP tool latency is reported by `/metrics`; most of it is the
  search itself, not the MCP layer.

## Alternatives

* **REST/OpenAPI only:** every assistant would re-implement tool wrappers, and policy
  would drift between them.
* **A bespoke function-calling plugin per assistant:** same drift, plus vendor lock-in.
