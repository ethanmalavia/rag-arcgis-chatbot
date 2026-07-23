"""Hybrid dense+sparse retrieval with RRF fusion, cross-encoder reranking, and recency."""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from langchain.schema import Document
from sentence_transformers import CrossEncoder

from config import (
    DENSE_K,
    ENABLE_PROJECT_SCOPE,
    ENABLE_RECENCY_BOOST,
    ENABLE_RERANKER,
    PROJECT_SCOPE_CAP,
    PROJECT_SCOPE_MIN_SUPPORT,
    RECENCY_BOOST,
    RECENCY_HALF_LIFE_DAYS,
    RERANK_CANDIDATES,
    RERANKER_MODEL,
    RERANK_K,
    SCORE_THRESHOLD,
    SPARSE_K,
)
from store import DataStore, _tokenize

_reranker: CrossEncoder | None = None
_YEAR_RE = re.compile(r"\b(20\d{2})\b")
_DATE_BODY_RE = re.compile(r"meeting_date:\s*(\d{4}-\d{2}-\d{2})", re.IGNORECASE)
_YEAR_BODY_RE = re.compile(r"meeting_year:\s*(20\d{2})", re.IGNORECASE)
_ISO_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def get_reranker() -> CrossEncoder:
    global _reranker
    if _reranker is None:
        print(f"Loading reranker {RERANKER_MODEL}…")
        _reranker = CrossEncoder(RERANKER_MODEL)
    return _reranker


def reciprocal_rank_fusion(rankings: list[list[str]], k: int = 60) -> list[tuple[str, float]]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda x: -x[1])


def document_meeting_date(doc: Document) -> date | None:
    """Resolve a meeting date from metadata or chunk text."""
    raw = str(doc.metadata.get("meeting_date") or "").strip()
    for candidate in (raw,):
        if not candidate:
            continue
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d"):
            try:
                return datetime.strptime(candidate[:10], fmt).date()
            except ValueError:
                continue
    text = doc.page_content or ""
    m = _DATE_BODY_RE.search(text) or _ISO_RE.search(text)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d").date()
        except ValueError:
            pass
    yraw = str(doc.metadata.get("meeting_year") or "").strip()
    ym = _YEAR_BODY_RE.search(text)
    year_s = yraw or (ym.group(1) if ym else "")
    if year_s.isdigit():
        try:
            return date(int(year_s), 6, 30)  # mid-year fallback
        except ValueError:
            return None
    return None


def recency_score(meeting: date | None, *, today: date | None = None) -> float:
    """1.0 ≈ today, decays toward 0 with age (exponential half-life)."""
    if meeting is None:
        return 0.25
    today = today or date.today()
    age_days = max(0, (today - meeting).days)
    half = max(1.0, RECENCY_HALF_LIFE_DAYS)
    return float(0.5 ** (age_days / half))


def apply_recency_boost(
    ranked: list[tuple[Document, float]],
    query: str,
    *,
    boost: float | None = None,
) -> list[tuple[Document, float]]:
    """Re-rank by relevance + recency (or prefer an explicit year in the query)."""
    if not ENABLE_RECENCY_BOOST or not ranked:
        return ranked
    weight = RECENCY_BOOST if boost is None else boost
    if weight <= 0:
        return ranked

    year_m = _YEAR_RE.search(query or "")
    target_year = int(year_m.group(1)) if year_m else None
    today = date.today()

    rescored: list[tuple[Document, float]] = []
    for doc, score in ranked:
        meeting = document_meeting_date(doc)
        if target_year is not None:
            # Historical query: prefer that year instead of "newest overall".
            if meeting and meeting.year == target_year:
                r = 1.0
            elif meeting and abs(meeting.year - target_year) <= 1:
                r = 0.45
            else:
                r = 0.1
        else:
            r = recency_score(meeting, today=today)
        # Keep original score on metadata for debugging.
        doc.metadata["recency"] = round(r, 4)
        if meeting:
            doc.metadata["meeting_date"] = meeting.isoformat()
        rescored.append((doc, float(score) + weight * r))
    return sorted(rescored, key=lambda x: -x[1])


def hybrid_retrieve(store: DataStore, query: str) -> list[tuple[Document, float]]:
    """Dense FAISS + BM25 via RRF, then optional cross-encoder rerank + recency."""
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
    doc_map = store.doc_by_id()

    candidates: list[Document] = []
    for doc_id, _ in fused[:RERANK_CANDIDATES]:
        if doc_id in doc_map:
            candidates.append(doc_map[doc_id])

    if not candidates:
        return apply_recency_boost([(d, float(s)) for d, s in dense_hits[:RERANK_K]], query)

    if not ENABLE_RERANKER:
        ranked = [(d, 1.0 - (i * 0.05)) for i, d in enumerate(candidates[: max(RERANK_K * 2, RERANK_K)])]
        return apply_recency_boost(ranked, query)[:RERANK_K]

    reranker = get_reranker()
    pairs = [(query, d.page_content) for d in candidates]
    scores = reranker.predict(pairs)
    # Always coerce to Python float — numpy.float32 is not JSON-serializable.
    ranked = [(d, float(s)) for d, s in sorted(zip(candidates, scores), key=lambda x: -float(x[1]))]
    filtered = [(d, s) for d, s in ranked if s >= SCORE_THRESHOLD]
    pool = filtered or ranked
    return apply_recency_boost(pool, query)[:RERANK_K]


def _project_id(doc: Document) -> str:
    return str(doc.metadata.get("project_id") or "").strip()


def dominant_project_id(
    hits: list[tuple[Document, float]], min_support: int
) -> str | None:
    """The project the hits converge on, by DISTINCT linked items (rows).

    Counts distinct row_index per project_id so several chunks of one item
    can't fake support. Returns None unless one project has >= min_support
    distinct items, so ordinary (non-project) queries are left untouched.
    """
    rows_by_pid: dict[str, set] = {}
    for doc, _ in hits:
        pid = _project_id(doc)
        if pid:
            rows_by_pid.setdefault(pid, set()).add(doc.metadata.get("row_index"))
    if not rows_by_pid:
        return None
    pid = max(rows_by_pid, key=lambda p: len(rows_by_pid[p]))
    return pid if len(rows_by_pid[pid]) >= min_support else None


def scope_hits_to_project(
    store: DataStore, hits: list[tuple[Document, float]]
) -> list[tuple[Document, float]]:
    """Focus retrieval on the project the hits converge on.

    Precision: drop hits belonging to a *different* non-empty project.
    Recall: pull in every item linked to the target project (its canonical
    'meta' chunk), so scattered actions (contracts, ordinances, updates) are
    all present. Unlinked keyword hits are kept but demoted below the linked
    set; grounding rules in the prompt prevent them being mis-attributed.
    """
    if not ENABLE_PROJECT_SCOPE or not hits:
        return hits
    pid = dominant_project_id(hits, PROJECT_SCOPE_MIN_SUPPORT)
    if not pid:
        return hits

    kept = [(d, s) for d, s in hits if _project_id(d) in ("", pid)]
    seen_rows = {d.metadata.get("row_index") for d, _ in kept}
    floor = min((s for _, s in kept), default=1.0)

    additions = [
        doc
        for doc in store.documents
        if _project_id(doc) == pid
        and doc.metadata.get("chunk_type") == "meta"
        and doc.metadata.get("row_index") not in seen_rows
    ]
    # Most-recent linked items first, so the cap keeps current activity when a
    # coarse bucket (e.g. a whole road) has more items than the cap.
    additions.sort(key=lambda d: (document_meeting_date(d) or date.min), reverse=True)
    for rank, doc in enumerate(additions):
        kept.append((doc, floor - 0.001 * (rank + 1)))

    # Target-project records first (retrieved hits by score, then recent linked
    # items); unlinked keyword hits demoted after and dropped if the cap fills.
    kept.sort(key=lambda t: (0 if _project_id(t[0]) == pid else 1, -t[1]))
    return kept[:PROJECT_SCOPE_CAP]


def format_docs(hits: list[tuple[Document, float]]) -> str:
    if not hits:
        return "No relevant records found in the dataset."
    return "\n\n--- RECORD ---\n\n".join(d.page_content for d, _ in hits)


def best_score(hits: list[tuple[Document, float]]) -> float:
    return float(max((float(s) for _, s in hits), default=0.0))


def hits_meta(hits: list[tuple[Document, float]]) -> dict[str, Any]:
    return {
        "retrieved": len(hits),
        "best_score": round(best_score(hits), 4),
        "chunk_ids": [d.metadata.get("chunk_id") for d, _ in hits],
        "meeting_dates": [d.metadata.get("meeting_date") for d, _ in hits],
    }
