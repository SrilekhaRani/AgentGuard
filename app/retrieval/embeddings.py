"""
embeddings.py — turn text into vectors.

The vector store only depends on the Embedder protocol, so the real
Hugging Face model can be swapped for a small fake one in tests.

Requires (for HuggingFaceEmbedder): sentence-transformers
The model is downloaded from Hugging Face on first use and cached locally.
"""

from __future__ import annotations

from typing import Any, Protocol

DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


class Embedder(Protocol):
    """Anything that can embed documents and queries."""

    @property
    def model_name(self) -> str: ...

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class HuggingFaceEmbedder:
    """
    Embeddings from a sentence-transformers model on Hugging Face.

    Vectors are normalized, so cosine similarity is a plain dot product.

    Some models expect a prefix on queries or documents. For example the
    BAAI/bge-*-en-v1.5 models work best with
        query_prefix="Represent this sentence for searching relevant passages: "
    all-MiniLM-L6-v2 (the default) needs no prefix.
    """

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL,
        *,
        device: str | None = None,
        batch_size: int = 32,
        query_prefix: str = "",
        document_prefix: str = "",
        model: Any | None = None,
    ) -> None:
        self._model_name = model_name
        self._batch_size = batch_size
        self._query_prefix = query_prefix
        self._document_prefix = document_prefix

        if model is None:
            # Imported here so the rest of the app (and the tests) don't
            # need torch installed just to import this module.
            from sentence_transformers import SentenceTransformer

            model = SentenceTransformer(model_name, device=device)
        self._model: Any = model

    @property
    def model_name(self) -> str:
        return self._model_name

    def _encode(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors = self._model.encode(
            texts,
            batch_size=self._batch_size,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return [[float(x) for x in row] for row in vectors]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._encode([self._document_prefix + t for t in texts])

    def embed_query(self, text: str) -> list[float]:
        return self._encode([self._query_prefix + text])[0]