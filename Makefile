comma := ,
.PHONY: install model db migrate ingest serve mcp mcp-demo search catalog generate test test-unit lint check down up image workflows demo-access-flow

install:
	uv sync

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
