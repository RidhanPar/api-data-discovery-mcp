"""Local embeddings with ONNX Runtime - no PyTorch, no network at query time.

Runs BAAI/bge-small-en-v1.5 (MIT licence, 384 dimensions) exported to ONNX and
int8-quantised. The same model as the sentence-transformers provider, but the
container image is ~1.5 GB smaller and cold starts are faster, which matters for
scale-to-zero hosting. Fetch the files with `make model`.
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

from .base import EmbeddingError

# BGE v1.5 recommends this instruction for short retrieval queries (not for passages).
BGE_QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "
MODEL_FILE = "model_quantized.onnx"
MAX_TOKENS = 512


class OnnxLocalEmbeddings:
    def __init__(self, model_dir: Path, *, dim: int = 384, batch_size: int = 32) -> None:
        onnx_path = model_dir / MODEL_FILE
        tok_path = model_dir / "tokenizer.json"
        if not onnx_path.exists() or not tok_path.exists():
            raise EmbeddingError(
                f"Embedding model not found in {model_dir}. Run `make model` to download it, "
                "or set NORDLYS_EMBEDDING_MODEL_DIR."
            )
        options = ort.SessionOptions()
        options.intra_op_num_threads = 0  # let ORT pick based on available cores
        self._session = ort.InferenceSession(str(onnx_path), options, providers=["CPUExecutionProvider"])
        self._input_names = {i.name for i in self._session.get_inputs()}
        self._tokenizer = Tokenizer.from_file(str(tok_path))
        self._tokenizer.enable_truncation(MAX_TOKENS)
        self._tokenizer.enable_padding()
        self._lock = threading.Lock()  # tokenizers padding config is not thread-safe
        self._dim = dim
        self._batch_size = batch_size
        digest = hashlib.sha256(onnx_path.read_bytes()).hexdigest()[:12]
        self._model_id = f"onnx:bge-small-en-v1.5-q:{digest}"

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dim(self) -> int:
        return self._dim

    def _embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start : start + self._batch_size]
            with self._lock:
                enc = self._tokenizer.encode_batch(batch)
            ids = np.array([e.ids for e in enc], dtype=np.int64)
            mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
            feeds = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self._input_names:
                feeds["token_type_ids"] = np.zeros_like(ids)
            hidden = self._session.run(None, feeds)[0]
            cls = hidden[:, 0]  # BGE uses the [CLS] token as the sentence embedding
            cls = cls / np.clip(np.linalg.norm(cls, axis=1, keepdims=True), 1e-12, None)
            if cls.shape[1] != self._dim:
                raise EmbeddingError(f"model produced {cls.shape[1]} dims, expected {self._dim}")
            out.extend(cls.astype(np.float32).tolist())
        return out

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._embed([BGE_QUERY_INSTRUCTION + text])[0]
