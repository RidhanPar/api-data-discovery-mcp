# api-data-discovery-mcp

An AI-powered discovery platform that makes the APIs and Data Products of **Nordlys Insurance**
(a fictional Nordic insurer) easy to discover, understand and access. It is exposed through the
**Model Context Protocol (MCP)**.

> Work in progress, built phase by phase. This README will grow into the full project write-up
> (architecture, measured results, security model, cost, limitations) in Phase 7.

## Status

| Phase | Scope | Status |
|---|---|---|
| 1 | Synthetic catalog: 25 OpenAPI 3.1 specs and 15 data contracts | ✅ done |
| 2 | Catalog service: FastAPI, Postgres/pgvector, hybrid search | ⏳ |
| 3 | MCP server: tools, resources, prompts | ⏳ |
| 4 | Security: OAuth 2.1/OIDC (Keycloak), policy as code, audit, injection defence | ⏳ |
| 5 | Discovery agent and evaluation | ⏳ |
| 6 | Docker, Azure (Container Apps, Postgres, Key Vault), observability | ⏳ |
| 7 | CI, docs, ADRs, demo | ⏳ |

## Quickstart (Phase 1)

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12.

```bash
make install     # uv sync
make catalog     # validate the catalog and print the summary report
make test        # pytest
make check       # lint + format check + mypy + generator drift + tests
```

See [`catalog/README.md`](catalog/README.md) for what is in the catalog and which imperfections
were added on purpose.
