"""Thin wrappers around the embedding and reranking models.

The main comparison uses ``BAAI/bge-small-en-v1.5`` because it is the smallest
of the three embedders used by Pesl et al. and it is the only one that can be
run on CPU without an API key. ``Qwen/Qwen3-Embedding-0.6B`` is MOSAIC's
production embedder and is available behind ``--embedder qwen`` for a second,
"as published" row.
"""

from __future__ import annotations

import numpy as np

BGE_MODEL = "BAAI/bge-small-en-v1.5"
QWEN_MODEL = "Qwen/Qwen3-Embedding-0.6B"
RERANKER_MODEL = "BAAI/bge-reranker-base"

# Prefixes follow db-gateway/db-gateway.py: Qwen3-Embedding is trained with
# asymmetric query/passage roles; BGE-small is used without prefixes, exactly
# as Pesl et al. instantiate it through LlamaIndex's HuggingFaceEmbedding.
_PREFIXES = {
    "bge": ("", ""),
    "qwen": ("query: ", "passage: "),
}


class Embedder:
    def __init__(self, name: str = "bge", device: str = "cpu") -> None:
        if name not in _PREFIXES:
            raise ValueError(f"unknown embedder {name!r}; choose from {sorted(_PREFIXES)}")
        if device != "cpu":
            raise ValueError("this harness is CPU-only by design")

        from sentence_transformers import SentenceTransformer

        self.name = name
        self.query_prefix, self.passage_prefix = _PREFIXES[name]
        if name == "bge":
            self.model = SentenceTransformer(BGE_MODEL, device=device)
        else:
            self.model = SentenceTransformer(
                QWEN_MODEL, device=device, trust_remote_code=True
            )

    def _encode(self, texts: list[str], prefix: str) -> np.ndarray:
        if not texts:
            return np.zeros((0, 1), dtype=np.float32)
        vectors = self.model.encode(
            [prefix + text for text in texts],
            batch_size=64,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return np.asarray(vectors, dtype=np.float32)

    def encode_queries(self, texts: list[str]) -> np.ndarray:
        return self._encode(texts, self.query_prefix)

    def encode_passages(self, texts: list[str]) -> np.ndarray:
        return self._encode(texts, self.passage_prefix)


class Reranker:
    """Cross-encoder used by MOSAIC's stage 2."""

    def __init__(self, device: str = "cpu") -> None:
        from sentence_transformers import CrossEncoder

        self.model = CrossEncoder(
            RERANKER_MODEL,
            device=device,
            max_length=256,  # endpoint texts are short; 256 keeps CPU cost sane
        )

    def score(self, query: str, documents: list[str], batch_size: int = 16) -> np.ndarray:
        if not documents:
            return np.zeros(0, dtype=np.float32)
        scores = self.model.predict(
            [(query, document) for document in documents],
            batch_size=batch_size,
            show_progress_bar=False,
        )
        return np.asarray(scores, dtype=np.float32).reshape(-1)
