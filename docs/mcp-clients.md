# Connecting MCP clients

The Nordlys discovery MCP server supports both standard transports:

| Transport | Start command | Use it from |
|---|---|---|
| Streamable HTTP | `make mcp` → `http://localhost:8001/mcp` | VS Code, Claude Code, the Python demo, any remote client |
| stdio | `python -m nordlys_discovery.mcp_server --transport stdio` | Claude Desktop (the client starts the process) |

Both need the catalog service running (`make ingest && make serve`).

**What was verified in this repository:** the Python client over Streamable HTTP
(`scripts/mcp_client_demo.py`), the stdio transport (launched with the SDK's stdio
client), and the contract tests in `tests/test_mcp_contract.py`. The Claude Desktop and
VS Code configurations below follow those products' documented formats, but I could not
run them in the build environment. If they don't work for you, open an issue.

## Python client (scripted demo)

```bash
make up && make mcp-demo        # full stack with Keycloak: signs in as a demo user

# or without an identity provider:
make serve                      # terminal 1
make mcp                        # terminal 2
uv run python scripts/mcp_client_demo.py --no-auth   # terminal 3
```

It runs: search → API details → endpoint schema → check_access → request_access. It also
shows three guard rails: reuse of the channel BFF is refused, a prohibited data-product
purpose is refused, and a malformed id is rejected by schema validation.

## Claude Desktop

Edit `claude_desktop_config.json` (macOS: `~/Library/Application Support/Claude/`,
Windows: `%APPDATA%\Claude\`), then restart Claude Desktop.

**Option A, stdio (recommended for local use):**

```json
{
  "mcpServers": {
    "nordlys-discovery": {
      "command": "uv",
      "args": [
        "--directory", "/ABSOLUTE/PATH/TO/api-data-discovery-mcp",
        "run", "python", "-m", "nordlys_discovery.mcp_server", "--transport", "stdio"
      ],
      "env": { "NORDLYS_CATALOG_API_URL": "http://localhost:8000" }
    }
  }
}
```

**Option B, the HTTP server through the `mcp-remote` bridge** (requires Node.js):

```json
{
  "mcpServers": {
    "nordlys-discovery": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://localhost:8001/mcp"]
    }
  }
}
```

Once the server is deployed behind HTTPS (Phase 6), it can be added in Claude as a
remote connector by URL instead.

Try: *"Which API gives me open claims for Norway, and how do I get access?"*

## VS Code (GitHub Copilot agent mode)

Create `.vscode/mcp.json` in your workspace:

```json
{
  "servers": {
    "nordlys-discovery": {
      "type": "http",
      "url": "http://localhost:8001/mcp"
    }
  }
}
```

Start the server from the code lens that appears in `mcp.json`, then open Copilot Chat in
**Agent** mode and enable the `nordlys-discovery` tools.

## Claude Code

```bash
claude mcp add --transport http nordlys-discovery http://localhost:8001/mcp
```

## Local identity (Phase 3 only)

Until Phase 4 adds OAuth, the server acts as a fixed development user:

```bash
NORDLYS_MCP_DEV_SUBJECT=dev.user@nordlys.example
NORDLYS_MCP_DEV_ASSET_SCOPES='["claims.search"]'   # pretend you already hold a scope
```

Phase 4 replaces this with identities and scopes taken from validated JWTs.
