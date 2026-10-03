from __future__ import annotations

from typing import Protocol, runtime_checkable


class EmbeddingError(RuntimeError):
    """The embedding provider is unavailable or returned something unusable."""


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Turns text into L2-normalised vectors of a fixed dimension.

    Documents and queries are separate calls because some models (BGE, E5) embed a
    search query differently from the passage it should match.
    """

    @property
    def model_id(self) -> str:
        """Stable identifier stored next to every vector; a change triggers re-embedding."""
        ...

    @property
    def dim(self) -> int: ...

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...
