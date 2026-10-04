# One image for the catalog service, MCP server, agent and ingestion job (different CMDs).

# The embedding model, byte-identical to `make model` (pinned by digest).
FROM semitechnologies/transformers-inference@sha256:301b962f1c7541f65db3f6922ee8376cc1a74361a338a0da57bc53e7e5a579d5 AS model

FROM python:3.12-slim AS build
# Optional corporate/proxy CA, passed as a BuildKit secret so it never lands in a layer:
#   docker build --secret id=ca_bundle,src=/path/to/ca.crt .
RUN --mount=type=secret,id=ca_bundle,required=false \
    if [ -f /run/secrets/ca_bundle ]; then export PIP_CERT=/run/secrets/ca_bundle; fi; \
    pip install --no-cache-dir uv==0.8.17
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=secret,id=ca_bundle,required=false \
    if [ -f /run/secrets/ca_bundle ]; then export SSL_CERT_FILE=/run/secrets/ca_bundle; fi; \
    uv sync --frozen --no-dev --no-install-project --extra azure --extra anthropic
COPY src ./src
RUN --mount=type=secret,id=ca_bundle,required=false \
    if [ -f /run/secrets/ca_bundle ]; then export SSL_CERT_FILE=/run/secrets/ca_bundle; fi; \
    uv sync --frozen --no-dev --no-editable --extra azure --extra anthropic

FROM python:3.12-slim
RUN useradd --system --uid 10001 --home /app app \
    && mkdir -p /data/registrations && chown -R app /data   # volume mount point, writable by the app user
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY --from=model /app/models/model /models/bge-small-en-v1.5-onnx-q
COPY alembic.ini ./
COPY migrations ./migrations
COPY catalog ./catalog
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    NORDLYS_CATALOG_DIR=/app/catalog \
    NORDLYS_EMBEDDING_MODEL_DIR=/models/bge-small-en-v1.5-onnx-q
USER app
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --retries=5 \
    CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=2)" || exit 1
CMD ["uvicorn", "nordlys_discovery.service.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
