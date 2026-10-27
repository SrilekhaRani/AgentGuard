"""
Tests for embeddings.py.

The first tests use a stand-in model, so they run anywhere.
The last test loads the real Hugging Face model. It is skipped when
sentence-transformers isn't installed; the first run downloads the model.

Run from the project root with:
    python -m pytest tests/test_embeddings.py -v
"""

from __future__ import annotations

import math

import pytest

from app.retrieval.embeddings import DEFAULT_MODEL, HuggingFaceEmbedder


class StandInModel:
    """Records what it was asked to encode and returns fixed vectors."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.kwargs: dict = {}

    def encode(self, texts, **kwargs):
        self.calls.append(list(texts))
        self.kwargs = kwargs
        return [[1.0, 0.0] for _ in texts]


def test_prefixes_are_applied():
    model = StandInModel()
    embedder = HuggingFaceEmbedder(
        "stand-in", model=model, query_prefix="query: ", document_prefix="passage: "
    )
    embedder.embed_documents(["restart pods"])
    embedder.embed_query("how to restart")
    assert model.calls == [["passage: restart pods"], ["query: how to restart"]]


def test_vectors_are_normalized_by_the_model():
    model = StandInModel()
    HuggingFaceEmbedder("stand-in", model=model).embed_documents(["x"])
    assert model.kwargs["normalize_embeddings"] is True


def test_empty_input_skips_the_model():
    model = StandInModel()
    assert HuggingFaceEmbedder("stand-in", model=model).embed_documents([]) == []
    assert model.calls == []


def test_returns_plain_python_floats():
    vectors = HuggingFaceEmbedder("stand-in", model=StandInModel()).embed_documents(["a", "b"])
    assert vectors == [[1.0, 0.0], [1.0, 0.0]]
    assert all(type(x) is float for row in vectors for x in row)


def test_real_model_ranks_related_text_higher():
    pytest.importorskip("sentence_transformers")
    embedder = HuggingFaceEmbedder(DEFAULT_MODEL)

    query = embedder.embed_query("how do I roll back a bad deployment")
    related, unrelated = embedder.embed_documents([
        "Revert the release to the previous version if errors increase.",
        "The cafeteria serves lunch from noon until two.",
    ])

    def cosine(a: list[float], b: list[float]) -> float:
        return sum(x * y for x, y in zip(a, b))

    assert len(query) == 384
    assert math.isclose(cosine(query, query), 1.0, rel_tol=1e-3)
    assert cosine(query, related) > cosine(query, unrelated)