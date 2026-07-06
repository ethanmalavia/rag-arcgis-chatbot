"""Hybrid dense+sparse retrieval with RRF fusion and cross-encoder reranking."""
from __future__ import annotations

from typing import Any

from langchain.schema import Document
from sentence_transformers import CrossEncoder

from config import DENSE_K, RERANKER_MODEL, RERANK_K, SCORE_THRESHOLD, SPARSE_K
from store import DataStore, _tokenize

_reranker: CrossEncoder | None = None


def get_reranker() -> CrossEncoder:
    global _reranker
    if _reranker is None:
        print(f"Loading reranker {RERANKER_MODEL}…")
        _reranker = CrossEncoder(RERANKER_MODEL)
    return _reranker


def _doc_by_id(store: DataStore) -> dict[str, Document]:
    return {d.metadata.get("chunk_id", str(i)): d for i, d in enumerate(store.documents)}


def reciprocal_rank_fusion(rankings: list[list[str]], k: int = 60) -> list[tuple[str, float]]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda x: -x[1])


def hybrid_retrieve(store: DataStore, query: str) -> list[tuple[Document, float]]:
    """Dense FAISS + BM25 via RRF, then cross-encoder rerank."""
    if store.vectorstore is None or store.bm25 is None:
        return []

    dense_hits = store.vectorstore.similarity_search_with_score(query, k=DENSE_K)
    dense_ranking = [d.metadata.get("chunk_id", "") for d, _ in dense_hits if d.metadata.get("chunk_id")]

    tokens = _tokenize(query)
    sparse_scores = store.bm25.get_scores(tokens)
    sparse_ranking = [
        store.bm25_ids[i]
        for i in sorted(range(len(sparse_scores)), key=lambda j: -sparse_scores[j])[:SPARSE_K]
    ]

    fused = reciprocal_rank_fusion([dense_ranking, sparse_ranking])
    doc_map = _doc_by_id(store)
    candidates: list[Document] = []
    for doc_id, _ in fused[: max(DENSE_K, SPARSE_K)]:
        if doc_id in doc_map:
            candidates.append(doc_map[doc_id])

    if not candidates:
        return [(d, float(s)) for d, s in dense_hits[:RERANK_K]]

    reranker = get_reranker()
    pairs = [(query, d.page_content) for d in candidates]
    scores = reranker.predict(pairs)
    ranked = sorted(zip(candidates, scores), key=lambda x: -float(x[1]))
    filtered = [(d, float(s)) for d, s in ranked if float(s) >= SCORE_THRESHOLD]
    if not filtered:
        filtered = ranked[:RERANK_K]
    else:
        filtered = filtered[:RERANK_K]
    return filtered


def format_docs(hits: list[tuple[Document, float]]) -> str:
    if not hits:
        return "No relevant records found in the dataset."
    return "\n\n--- RECORD ---\n\n".join(d.page_content for d, _ in hits)


def best_score(hits: list[tuple[Document, float]]) -> float:
    return max((s for _, s in hits), default=0.0)


def hits_meta(hits: list[tuple[Document, float]]) -> dict[str, Any]:
    return {
        "retrieved": len(hits),
        "best_score": round(best_score(hits), 4),
        "chunk_ids": [d.metadata.get("chunk_id") for d, _ in hits],
    }
