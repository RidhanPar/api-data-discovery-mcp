# ADR 0004: Keycloak for local and demo environments, Entra ID in a real Azure tenant

**Status:** accepted

## Context

The services need OAuth 2.1/OIDC: user tokens with audiences and scopes for the MCP
server, client credentials for service-to-service calls, and a way for a workflow to
grant a role after approval. The project must run fully offline on a laptop and in CI,
and also deploy to Azure, where the obvious identity provider is Entra ID.

## Decision

Use **Keycloak 26** with the realm defined as code (`deploy/keycloak/nordlys-realm.json`)
for local runs, tests and the demo Azure deployment. Keep the services provider-neutral:
they validate standard JWTs (issuer, audience, signature from JWKS, expiry) and map
claims to a `Principal` in one place (`security/jwt.py`). Document the Entra ID mapping
(app registrations, delegated scopes, app roles, Graph app-role assignments,
on-behalf-of) in `docs/azure.md`.

## Consequences

+ Real OAuth in every test and demo, with no cloud tenant or licence needed; security
  integration tests run against an actual Keycloak.
+ Moving to Entra ID is configuration plus claim mapping (`scp` vs `scope`, app roles),
  not a rewrite.
- Two identity setups to understand. Keycloak in Azure is one more container to patch;
  in a real tenant it would be removed.
- Per-user delegation from the agent to the MCP server (token exchange) is not
  implemented: the agent uses its own least-privilege identity (see
  `docs/security.md`). Entra ID's on-behalf-of flow is the production answer.

## Alternatives

* **Entra ID only:** no offline development or CI without a tenant; tests would mock the
  IdP, which is exactly where security bugs hide.
* **No IdP, API keys:** no user identity, no scopes, no audit of who asked.
