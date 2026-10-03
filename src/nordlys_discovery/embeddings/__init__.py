"""Embedding providers behind one small interface."""

from .base import EmbeddingError, EmbeddingProvider
from .factory import create_provider

__all__ = ["EmbeddingError", "EmbeddingProvider", "create_provider"]
