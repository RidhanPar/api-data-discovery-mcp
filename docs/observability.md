# Observability

Kept deliberately small: structured logs that every environment already collects, plus a
per-process metrics endpoint. No extra agents or collectors to run or pay for.

## Logs

Every service writes one JSON object per line to stdout (`NORDLYS_LOG_JSON=false` for
plain text). Locally: `docker compose logs mcp --no-log-prefix | jq`. In Azure, Container
Apps ship stdout to Log Analytics (`ContainerAppConsoleLogs_CL`).

| Event (`msg`) | Service | Fields |
|---|---|---|
| `tool_call` | mcp | `tool`, `decision` (allowed / denied / invalid / error), `latency_ms`, `zero_results` (search only), `client_id`, `request_id` |
| `http_request` | catalog | `method`, `route` (template, e.g. `/v1/apis/{api_id}/v{major}`), `status`, `latency_ms`, `request_id` |
| `agent_answer` | agent | `model` (the model that actually answered), `stop`, `refused`, `tool_calls`, `ungrounded_citations`, `input_tokens`, `output_tokens`, `latency_ms` |

**Correlation.** The MCP guard gives every tool call a fresh `request_id`, sends it to
the catalog as `X-Request-ID` and writes it into the audit event, so one id links the
MCP log line, the catalog log lines and the audit row. Callers of the catalog's HTTP
API can pass their own `X-Request-ID`; it is echoed in the response and in error bodies.

Who did what is in the **audit log** (append-only table, `GET /v1/audit-events`), not
in application logs; logs carry the client id, not the end user.

## Metrics endpoint

`GET /metrics` on the MCP server (`:8001`) and the catalog (`:8000`): per tool / per
route call count, server-error rate, p50/p95 latency over the last 1,000 calls, and for
`search_catalog` the zero-result rate. It is per process; with several replicas use the
log queries below instead.

Note on the zero-result rate: hybrid search always returns the nearest vectors, so it is
only non-zero when filters exclude everything. A "nothing relevant" signal would need a
relevance threshold, which the agent eval does not yet justify; not implemented.

## Log Analytics queries (Azure dashboard)

Pin these to an Azure dashboard or a workbook. `Log_s` holds the JSON line.

Tool latency and error rate, last 24 h:

```kusto
ContainerAppConsoleLogs_CL
| where TimeGenerated > ago(24h) and ContainerAppName_s == "mcp"
| extend e = parse_json(Log_s)
| where e.msg == "tool_call"
| summarize calls = count(),
            p50_ms = percentile(todouble(e.latency_ms), 50),
            p95_ms = percentile(todouble(e.latency_ms), 95),
            error_rate = round(countif(e.decision == "error") * 1.0 / count(), 4),
            denied = countif(e.decision == "denied")
  by tool = tostring(e.tool)
| order by calls desc
```

Zero-result searches (filters that match nothing), per hour:

```kusto
ContainerAppConsoleLogs_CL
| where ContainerAppName_s == "mcp"
| extend e = parse_json(Log_s)
| where e.msg == "tool_call" and e.tool == "search_catalog" and e.decision == "allowed"
| summarize zero_result_rate = countif(tobool(e.zero_results)) * 1.0 / count() by bin(TimeGenerated, 1h)
| render timechart
```

Agent cost and quality signals per model, per day:

```kusto
ContainerAppConsoleLogs_CL
| where ContainerAppName_s == "agent"
| extend e = parse_json(Log_s)
| where e.msg == "agent_answer"
| summarize answers = count(),
            refused = countif(tobool(e.refused)),
            ungrounded = sum(toint(e.ungrounded_citations)),
            tokens_in = sum(toint(e.input_tokens)), tokens_out = sum(toint(e.output_tokens)),
            p95_ms = percentile(todouble(e.latency_ms), 95)
  by bin(TimeGenerated, 1d), model = tostring(e.model)
```

Follow one request across services:

```kusto
ContainerAppConsoleLogs_CL
| extend e = parse_json(Log_s)
| where tostring(e.request_id) == "<id>"
| project TimeGenerated, ContainerAppName_s, e.msg, e
```

Suggested alerts (not provisioned by Terraform yet): tool `error_rate > 5%` over 15
min; any `agent_answer` with `ungrounded_citations > 0`; any `tool_call` with
`decision == "denied"` spike, which can indicate a misconfigured client or probing.

## Health and resilience

* `/health/live` (process up) and `/health/ready` (dependencies reachable: DB and
  embedding model for the catalog, catalog for the MCP server) on every service; used by
  docker-compose health checks and Container Apps probes.
* MCP server → catalog: timeouts and bounded retries with backoff for idempotent calls
  only (`catalog_retries`); LLM calls: SDK retries with backoff on 429/5xx.
* n8n workflows: retries on HTTP nodes, timeouts on human waits, and an error-handler
  workflow (see docs/n8n.md).
