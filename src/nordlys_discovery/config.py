"""Runtime configuration, read from environment variables (12-factor).

Every setting has a safe local default so `make up` works without a .env file.
Secrets (DB password, Azure keys) come from the environment, which in Azure is
populated from Key Vault - never from files in the repo.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

EmbeddingProviderName = Literal["onnx-local", "sentence-transformers", "azure-openai", "hashing"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NORDLYS_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://nordlys:nordlys@localhost:5432/nordlys"
    db_pool_size: int = 5
    db_statement_timeout_ms: int = 5000

    catalog_dir: Path = Path(__file__).resolve().parents[2] / "catalog"
    # APIs published through Workflow B; ingested together with catalog_dir.
    registrations_dir: Path | None = None

    # Embeddings. The vector column is fixed at `embedding_dim`; every provider must
    # produce vectors of this size (Azure text-embedding-3-* supports a `dimensions` arg).
    embedding_provider: EmbeddingProviderName = "onnx-local"
    embedding_dim: int = 384
    embedding_model_dir: Path = Path.home() / ".cache" / "nordlys-models" / "bge-small-en-v1.5-onnx-q"
    embedding_batch_size: int = 32
    sentence_transformers_model: str = "BAAI/bge-small-en-v1.5"

    azure_openai_endpoint: str | None = None
    azure_openai_api_key: SecretStr | None = None
    azure_openai_api_version: str = "2024-10-21"
    azure_openai_embedding_deployment: str = "text-embedding-3-small"

    # LLM (agent, registration classifier, LLM judge). "none" = no generative steps;
    # workflows then route everything to human review.
    llm_provider: Literal["azure-openai", "anthropic", "openai", "none"] = "none"
    llm_timeout_s: float = 60.0
    azure_openai_chat_deployment: str = "gpt-4.1-mini"
    anthropic_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("NORDLYS_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY")
    )
    anthropic_model: str = "claude-opus-5-5"
    anthropic_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    anthropic_fallbacks: bool = True  # server-side refusal fallbacks (beta)
    # Claude Platform on AWS (Anthropic-operated, AWS billing): requests are routed by
    # workspace id and region. Setting the workspace id switches the Anthropic provider to
    # it. Auth: ANTHROPIC_API_KEY holding an AWS short-term API key, or AWS credentials
    # (SigV4) from the usual AWS environment/profile chain when no key is set.
    anthropic_aws_workspace_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("NORDLYS_ANTHROPIC_AWS_WORKSPACE_ID", "ANTHROPIC_AWS_WORKSPACE_ID"),
    )
    aws_region: str | None = Field(default=None, validation_alias=AliasChoices("NORDLYS_AWS_REGION", "AWS_REGION"))
    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-4.1-mini"

    # Search.
    search_candidates: int = Field(50, description="Hits taken from each retriever before fusion.")
    rrf_k: int = Field(60, description="Reciprocal-rank-fusion constant (Cormack et al., 2009).")
    bm25_k1: float = 1.2
    bm25_b: float = 0.75
    search_deprecated_penalty: float = 0.5

    # MCP server (Phase 3).
    catalog_api_url: str = "http://localhost:8000"
    catalog_timeout_s: float = 5.0
    catalog_retries: int = 2
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8001
    # Local-development caller identity. Phase 4 replaces this with validated JWT claims.
    mcp_dev_subject: str = "dev.user@nordlys.example"
    mcp_dev_asset_scopes: list[str] = Field(default_factory=list)

    # Security (Phase 4). Off by default so unit tests and quick local runs need no IdP;
    # docker-compose and Azure turn it on.
    auth_enabled: bool = False
    oidc_issuer: str = "http://localhost:8080/realms/nordlys"
    oidc_jwks_url: str | None = None  # internal URL when the issuer's public URL is not reachable
    catalog_audience: str = "nordlys-catalog"
    mcp_audience: str = "nordlys-mcp"
    agent_audience: str = "nordlys-agent"
    mcp_public_url: str = "http://localhost:8001/mcp"
    # Services allowed to call the catalog on behalf of an end user (X-On-Behalf-Of-* headers).
    trusted_service_clients: list[str] = Field(default_factory=lambda: ["nordlys-mcp-server", "nordlys-n8n"])
    # The MCP server's own credentials for calling the catalog (client credentials grant).
    mcp_client_id: str = "nordlys-mcp-server"
    mcp_client_secret: SecretStr | None = None
    rate_limit_per_minute: int = 60
    rate_limit_burst: int = 20
    # Agent service -> MCP server. The agent calls MCP with its own service identity
    # (client credentials), which holds no asset scopes: it only ever sees what any
    # employee may see. Per-user delegation (RFC 8693 token exchange) is documented in
    # docs/security.md as the next step.
    agent_mcp_url: str = "http://localhost:8001/mcp"
    agent_client_id: str = "nordlys-discovery-agent"
    agent_client_secret: SecretStr | None = None
    agent_max_steps: int = 8
    # Workflow A: request_access notifies n8n (empty = no workflow, request just stays pending).
    access_request_webhook_url: str | None = None
    access_request_webhook_secret: SecretStr | None = None

    log_level: str = "INFO"
    log_json: bool = True  # one JSON object per line; false = plain text for local reading


@lru_cache
def get_settings() -> Settings:
    return Settings()
