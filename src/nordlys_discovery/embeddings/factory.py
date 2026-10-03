from __future__ import annotations

from ..config import Settings
from .base import EmbeddingError, EmbeddingProvider
from .onnx_local import OnnxLocalEmbeddings
from .others import AzureOpenAIEmbeddings, HashingEmbeddings, SentenceTransformersEmbeddings


def create_provider(settings: Settings) -> EmbeddingProvider:
    match settings.embedding_provider:
        case "onnx-local":
            return OnnxLocalEmbeddings(
                settings.embedding_model_dir, dim=settings.embedding_dim, batch_size=settings.embedding_batch_size
            )
        case "sentence-transformers":
            return SentenceTransformersEmbeddings(settings.sentence_transformers_model, dim=settings.embedding_dim)
        case "azure-openai":
            if not settings.azure_openai_endpoint:
                raise EmbeddingError("NORDLYS_AZURE_OPENAI_ENDPOINT is required")
            key = settings.azure_openai_api_key
            return AzureOpenAIEmbeddings(
                endpoint=settings.azure_openai_endpoint,
                api_key=key.get_secret_value() if key else None,  # None -> managed identity
                api_version=settings.azure_openai_api_version,
                deployment=settings.azure_openai_embedding_deployment,
                dim=settings.embedding_dim,
            )
        case "hashing":
            return HashingEmbeddings(settings.embedding_dim)
    raise EmbeddingError(f"unknown embedding provider {settings.embedding_provider!r}")  # pragma: no cover
