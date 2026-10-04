comma := ,
.PHONY: install model db migrate ingest serve mcp mcp-demo search catalog generate test test-unit lint check down up image workflows demo-access-flow eval-retrieval eval-retrieval-ci eval-agent azure-test azure-cost azure-up azure-down

install:
	uv sync --all-extras  # the azure and anthropic LLM providers, as CI and the image install them

model:            ## download + verify the local embedding model (needs Docker)
	uv run python scripts/fetch_model.py

db:               ## start Postgres + pgvector and wait until healthy
	docker compose up -d --wait db

migrate: db
	uv run alembic upgrade head

ingest: migrate   ## idempotent: re-running only processes changes
	uv run python -m nordlys_discovery.ingest

serve:            ## catalog service on http://localhost:8000/docs
	uv run uvicorn nordlys_discovery.service.app:app --port 8000

mcp:              ## MCP server, Streamable HTTP on http://localhost:8001/mcp (needs `make serve`)
	uv run python -m nordlys_discovery.mcp_server

mcp-demo:         ## scripted MCP client walk-through against the running server
	uv run python scripts/mcp_client_demo.py

search:           ## make search Q="open claims in Norway"
	uv run python -m nordlys_discovery.search "$(Q)"

catalog:
	uv run python -m nordlys_discovery.catalog

generate:
	uv run python -m scripts.catalog_gen

test:             ## all tests; integration tests start a pgvector container
	uv run pytest

test-unit:
	uv run pytest -m "not integration"

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

check: lint
	uv run python -m scripts.catalog_gen --check
	uv run python deploy/n8n/build_workflows.py --check
	uv run python deploy/keycloak/sync_roles.py --check
	uv run pytest

image:            ## build the app image (works behind a TLS-intercepting proxy: CA passed as a build secret)
	DOCKER_BUILDKIT=1 docker build $(if $(CA_BUNDLE),--secret id=ca_bundle$(comma)src=$(CA_BUNDLE)) $(DOCKER_BUILD_ARGS) -t nordlys-discovery:dev .

up: image         ## full local stack: catalog, MCP, Keycloak, n8n, Mailpit, Postgres
	docker compose up -d --wait

down:
	docker compose down

workflows:        ## regenerate the n8n workflow JSON from deploy/n8n/build_workflows.py
	uv run python deploy/n8n/build_workflows.py

demo-access-flow: ## Workflow A end to end: request over MCP -> e-mail -> approve -> role granted
	uv run python scripts/demo_access_flow.py --reset

eval-retrieval:   ## retrieval eval (no LLM): lexical vs vector vs hybrid, +/- deprecation demotion
	uv run python -m eval.retrieval

eval-retrieval-ci: ## CI regression gate on the small subset
	uv run python -m eval.retrieval --subset ci --min-recall3 0.75 --min-mrr 0.75

eval-agent:       ## agent eval with an LLM judge (needs NORDLYS_LLM_PROVIDER + credentials)
	uv run python -m eval.agent_eval --judge

azure-test:       ## offline Terraform checks: fmt, validate, plan tests with mocked providers
	cd infra && terraform fmt -check && terraform init -input=false -backend=false >/dev/null && terraform validate && terraform test

azure-cost:       ## monthly estimate from live Azure list prices (Retail Prices API)
	uv run python infra/cost_estimate.py

azure-up:         ## deploy everything to Azure (needs az login + infra/terraform.tfvars)
	scripts/azure_up.sh

azure-down:       ## destroy everything in Azure
	scripts/azure_down.sh
