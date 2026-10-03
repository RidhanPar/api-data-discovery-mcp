"""Alternative providers: sentence-transformers, Azure OpenAI, and a test-only hasher."""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any

from .base import EmbeddingError
from .onnx_local import BGE_QUERY_INSTRUCTION


class SentenceTransformersEmbeddings:
    """PyTorch path for the same BGE model. Install with `uv sync --extra st`.

    Not the default: it pulls in PyTorch (~2 GB image) for no quality gain here.
    """

    def __init__(self, model_name: str, *, dim: int = 384) -> None:
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise EmbeddingError("sentence-transformers is not installed: uv sync --extra st") from exc
        self._model: Any = SentenceTransformer(model_name, device="cpu")
        self._name = model_name
        self._dim = dim

    @property
    def model_id(self) -> str:
        return f"st:{self._name}"

    @property
    def dim(self) -> int:
        return self._dim

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors = self._model.encode(texts, normalize_embeddings=True, convert_to_numpy=True)
        return [list(map(float, v)) for v in vectors]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([BGE_QUERY_INSTRUCTION + text])[0]


class AzureOpenAIEmbeddings:
    """Azure OpenAI text-embedding-3-*, truncated to `dim` via the `dimensions` parameter.

    Keeping 384 dimensions means switching provider needs re-embedding, not a schema
    migration. Install with `uv sync --extra azure`.
    """

    def __init__(self, *, endpoint: str, api_key: str, api_version: str, deployment: str, dim: int = 384) -> None:
        try:
            from openai import AzureOpenAI
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise EmbeddingError("openai is not installed: uv sync --extra azure") from exc
        self._client = AzureOpenAI(
            azure_endpoint=endpoint, api_key=api_key, api_version=api_version, max_retries=3, timeout=20.0
        )
        self._deployment = deployment
        self._dim = dim

    @property
    def model_id(self) -> str:
        return f"azure-openai:{self._deployment}:{self._dim}"

    @property
    def dim(self) -> int:
        return self._dim

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for start in range(0, len(texts), 64):
            resp = self._client.embeddings.create(
                model=self._deployment, input=texts[start : start + 64], dimensions=self._dim
            )
            out.extend(d.embedding for d in sorted(resp.data, key=lambda d: d.index))
        return out

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


class HashingEmbeddings:
    """Deterministic bag-of-words hashing vectors. FOR TESTS ONLY - no semantics.

    Lets unit and integration tests exercise the vector code path without
    downloading a model. Never use it to report search quality.
    """

    def __init__(self, dim: int = 384) -> None:
        self._dim = dim

    @property
    def model_id(self) -> str:
        return f"hashing:{self._dim}"

    @property
    def dim(self) -> int:
        return self._dim

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * self._dim
        for token in re.findall(r"[a-z0-9]+", text.lower()):
            h = int.from_bytes(hashlib.blake2b(token.encode(), digest_size=8).digest(), "big")
            vec[h % self._dim] += 1.0 if (h >> 63) & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._one(text)
