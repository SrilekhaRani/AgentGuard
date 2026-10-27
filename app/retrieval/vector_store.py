"""
vector_store.py — store chunk embeddings in ChromaDB and search them.

Security rules built into this module:
  - Every search needs a team. There is no way to search across tenants,
    so one team can never retrieve another team's documents.
  - Each result carries its trust_level, so the guardrail layer can decide
    whether retrieved text may influence the agent's actions.
  - A collection remembers which embedding model filled it. Opening it with
    a different model raises an error, because vectors from two models
    can't be compared and search would silently return nonsense.

Re-indexing a file replaces its old chunks, so edited documents don't
leave stale chunks behind.

Requires: chromadb
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import chromadb

from app.ingestion.models import Chunk, Environment, SearchResult, TrustLevel
from app.retrieval.embeddings import Embedder

logger = logging.getLogger(__name__)

DEFAULT_COLLECTION = "agentguard_chunks"
_MODEL_KEY = "embedding_model"


def _where(conditions: list[dict[str, Any]]) -> dict[str, Any]:
    """Combine filter conditions. Chroma's $and needs at least two items."""
    return conditions[0] if len(conditions) == 1 else {"$and": conditions}


def _chunk_metadata(chunk: Chunk) -> dict[str, str | int | bool]:
    return {
        "doc_id": chunk.doc_id,
        "chunk_index": chunk.chunk_index,
        "heading": chunk.heading,
        "source_path": chunk.source_path,
        "team": chunk.team,
        "service": chunk.service,
        "env": chunk.env.value,
        "doc_type": chunk.doc_type.value,
        "trust_level": chunk.trust_level.value,
        "redacted": chunk.redacted,
    }


class VectorStore:
    """A ChromaDB collection of chunks, searched one team at a time."""

    def __init__(
        self,
        embedder: Embedder,
        path: str | Path | None = None,
        collection_name: str = DEFAULT_COLLECTION,
        batch_size: int = 64,
    ) -> None:
        """
        path: folder where Chroma saves its data. None keeps everything in
              memory, which is only useful for quick experiments.
        """
        self._embedder = embedder
        self._batch_size = batch_size
        self._client = (
            chromadb.PersistentClient(path=str(path)) if path is not None
            else chromadb.EphemeralClient()
        )

        existing = {c.name for c in self._client.list_collections()}
        if collection_name in existing:
            self._collection = self._client.get_collection(collection_name)
            stored_model = (self._collection.metadata or {}).get(_MODEL_KEY)
            if stored_model != embedder.model_name:
                raise ValueError(
                    f"Collection {collection_name!r} was built with {stored_model!r}, "
                    f"not {embedder.model_name!r}. Rebuild the index or use the same model."
                )
        else:
            self._collection = self._client.create_collection(
                collection_name,
                metadata={_MODEL_KEY: embedder.model_name},
                configuration={"hnsw": {"space": "cosine"}},
            )

    # ----------------------------------------------------------------------
    # Writing
    # ----------------------------------------------------------------------

    def add_chunks(self, chunks: Iterable[Chunk]) -> int:
        """
        Add chunks, replacing any older chunks from the same files.
        Returns how many chunks were stored.
        """
        chunks = list(chunks)
        if not chunks:
            return 0

        # Drop old chunks for every (team, file) in this batch first, so an
        # edited runbook doesn't keep its outdated steps in the index.
        for team, source_path in {(c.team, c.source_path) for c in chunks}:
            self._collection.delete(
                where=_where([{"team": team}, {"source_path": source_path}])
            )

        for start in range(0, len(chunks), self._batch_size):
            batch = chunks[start:start + self._batch_size]
            vectors = self._embedder.embed_documents([c.embedding_text for c in batch])
            self._collection.upsert(
                ids=[c.chunk_id for c in batch],
                embeddings=vectors,  # type: ignore[arg-type]
                documents=[c.text for c in batch],
                metadatas=[_chunk_metadata(c) for c in batch],  # type: ignore[arg-type]
            )

        logger.info("Indexed %d chunks", len(chunks))
        return len(chunks)

    def delete_file(self, team: str, source_path: str) -> None:
        """Remove every chunk of one file for one team."""
        self._collection.delete(
            where=_where([{"team": team.strip().lower()}, {"source_path": source_path}])
        )

    # ----------------------------------------------------------------------
    # Reading
    # ----------------------------------------------------------------------

    def count(self, team: str | None = None) -> int:
        """Number of stored chunks, for one team or in total."""
        if team is None:
            return self._collection.count()
        found = self._collection.get(where={"team": team.strip().lower()}, include=[])
        return len(found["ids"])

    def search(
        self,
        query: str,
        *,
        team: str,
        k: int = 5,
        service: str | None = None,
        env: Environment | None = None,
        trust_levels: Sequence[TrustLevel] | None = None,
    ) -> list[SearchResult]:
        """
        Return the k most relevant chunks for one team, best first.

        team is required: results only ever come from that team's documents.
        trust_levels limits results, e.g. [TrustLevel.TRUSTED] for steps the
        agent is allowed to act on.
        """
        team = team.strip().lower()
        if not team:
            raise ValueError("team must not be blank")
        if not query.strip():
            raise ValueError("query must not be blank")
        if k <= 0:
            raise ValueError("k must be positive")

        conditions: list[dict[str, Any]] = [{"team": team}]
        if service is not None:
            conditions.append({"service": service.strip().lower()})
        if env is not None:
            conditions.append({"env": env.value})
        if trust_levels is not None:
            if not trust_levels:
                return []
            conditions.append({"trust_level": {"$in": [t.value for t in trust_levels]}})

        found = self._collection.query(
            query_embeddings=[self._embedder.embed_query(query)],  # type: ignore[arg-type]
            n_results=k,
            where=_where(conditions),
            include=["documents", "metadatas", "distances"],
        )

        ids = found["ids"][0]
        documents = (found["documents"] or [[]])[0]
        metadatas = (found["metadatas"] or [[]])[0]
        distances = (found["distances"] or [[]])[0]

        results: list[SearchResult] = []
        for chunk_id, text, meta, distance in zip(ids, documents, metadatas, distances):
            results.append(
                SearchResult.model_validate(
                    {
                        **meta,
                        "chunk_id": chunk_id,
                        "text": text,
                        # Chroma returns cosine distance; similarity = 1 - distance.
                        "score": 1.0 - float(distance),
                    }
                )
            )
        return results