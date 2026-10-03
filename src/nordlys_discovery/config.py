"""Runtime configuration, read from environment variables (12-factor).

Every setting has a safe local default so `make up` works without a .env file.
Secrets (DB password, Azure keys) come from the environment, which in Azure is
populated from Key Vault - never from files in the repo.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

EmbeddingProviderName = Literal["onnx-local", "sentence-transformers", "azure-openai", "hashing"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NORDLYS_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://nordlys:nordlys@localhost:5432/nordlys"
    db_pool_size: int = 5
    db_statement_timeout_ms: int = 5000

    catalog_dir: Path = Path(__file__).resolve().parents[2] / "catalog"

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

    # Search.
    search_candidates: int = Field(50, description="Hits taken from each retriever before fusion.")
    rrf_k: int = Field(60, description="Reciprocal-rank-fusion constant (Cormack et al., 2009).")
    bm25_k1: float = 1.2
    bm25_b: float = 0.75

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
