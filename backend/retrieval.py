"""Hybrid dense+sparse retrieval with RRF fusion, cross-encoder reranking, and recency."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

from langchain_core.documents import Document
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
    RECENT_QUERY_BOOST,
    RECENT_QUERY_MAX_AGE_YEARS,
    RERANK_CANDIDATES,
    RERANKER_MODEL,
    RERANK_K,
    SCORE_THRESHOLD,
    SPARSE_K,
)
from store import DataStore, _tokenize

_reranker: CrossEncoder | None = None
# Cap rerank input length — tokenizer max is ~512 tokens anyway; long article
# chunks otherwise dominate CPU time (same rationale as backend/reranker.py).
_MAX_CHARS_PER_DOC = 1200
_YEAR_RE = re.compile(r"\b(20\d{2})\b")
_DATE_BODY_RE = re.compile(r"meeting_date:\s*(\d{4}-\d{2}-\d{2})", re.IGNORECASE)
_YEAR_BODY_RE = re.compile(r"meeting_year:\s*(20\d{2})", re.IGNORECASE)
_ISO_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
# Conversational recency cues ("recent developments", "anything new", …).
# Explicit years are handled separately as historical intent.
_RECENT_QUERY_RE = re.compile(
    r"\b("
    r"recent(?:ly)?|latest|newest|lately|nowadays|"
    r"this\s+year|last\s+year|past\s+(?:year|few\s+years?|months?)|"
    r"last\s+(?:few\s+)?(?:years?|months?)|"
    r"new(?:er)?|current(?:ly)?"
    r")\b",
    re.IGNORECASE,
)


def query_wants_recent(query: str) -> bool:
    """True when the question asks for recent/new/latest (and names no year)."""
    q = (query or "").strip()
    if not q or _YEAR_RE.search(q):
        return False
    return bool(_RECENT_QUERY_RE.search(q))


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


_DATE_HEADER_RE = re.compile(r"^DATE:\s*(\d{4}-\d{2}-\d{2})", re.IGNORECASE | re.MULTILINE)


def _parse_iso_like_date(raw: str) -> date | None:
    text = (raw or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    return None


def document_meeting_date(doc: Document) -> date | None:
    """Resolve a source date from metadata or chunk text.

    Covers board/council ``meeting_date`` and EsteroToday ``publish_date`` /
    generic ``date`` so article chunks get the same recency treatment.
    """
    for key in ("meeting_date", "publish_date", "date"):
        parsed = _parse_iso_like_date(str(doc.metadata.get(key) or ""))
        if parsed:
            return parsed

    text = doc.page_content or ""
    header = _DATE_HEADER_RE.search(text)
    if header:
        parsed = _parse_iso_like_date(header.group(1))
        if parsed:
            return parsed
    m = _DATE_BODY_RE.search(text) or _ISO_RE.search(text)
    if m:
        parsed = _parse_iso_like_date(m.group(1))
        if parsed:
            return parsed
    yraw = str(doc.metadata.get("meeting_year") or "").strip()
    ym = _YEAR_BODY_RE.search(text)
    year_s = yraw or (ym.group(1) if ym else "")
    if year_s.isdigit():
        try:
            return date(int(year_s), 6, 30)  # mid-year fallback
        except ValueError:
            return None
    return None


def sort_hits_newest_first(
    ranked: list[tuple[Document, float]],
    *,
    by_date_primary: bool = False,
) -> list[tuple[Document, float]]:
    """Order hits so newer sources come first.

    by_date_primary=False (ranking): score primary, newest date breaks ties.
    by_date_primary=True (context/citations): newest dated sources first so
    the model references recent articles before older ones.
    """
    if len(ranked) < 2:
        return ranked

    def _key(item: tuple[Document, float]) -> tuple:
        doc, score = item
        d = document_meeting_date(doc)
        date_key = -(d.toordinal()) if d else 0
        if by_date_primary:
            return (0 if d else 1, date_key, -float(score))
        return (-float(score), date_key)

    return sorted(ranked, key=_key)


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
    intent_query: str | None = None,
) -> list[tuple[Document, float]]:
    """Re-rank by relevance + recency (or prefer an explicit year in the query).

    intent_query carries the citizen's original question for temporal intent so
    CRAG rewrites that inject year-like tokens cannot disable recent-mode.
    """
    if not ENABLE_RECENCY_BOOST or not ranked:
        return ranked

    intent = intent_query if intent_query is not None else query
    year_m = _YEAR_RE.search(intent or "")
    target_year = int(year_m.group(1)) if year_m else None
    wants_recent = target_year is None and query_wants_recent(intent)
    if boost is None:
        weight = RECENT_QUERY_BOOST if wants_recent else RECENCY_BOOST
    else:
        weight = boost
    today = date.today()
    if weight <= 0:
        return prefer_recent_hits(ranked, intent, today=today)

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
            # Normalize into meeting_date for downstream meta/cards; articles
            # also land here via publish_date resolution above.
            doc.metadata["meeting_date"] = meeting.isoformat()
            if not doc.metadata.get("date"):
                doc.metadata["date"] = meeting.isoformat()
        rescored.append((doc, float(score) + weight * r))
    rescored = sort_hits_newest_first(rescored)
    return prefer_recent_hits(rescored, intent, today=today)


def prefer_recent_hits(
    ranked: list[tuple[Document, float]],
    query: str,
    *,
    today: date | None = None,
    max_age_years: float | None = None,
) -> list[tuple[Document, float]]:
    """When the question asks for recent items, drop old hits.

    Never restores known-old dated hits: fresh → undated → empty.
    """
    if not ranked or not query_wants_recent(query):
        return ranked
    years = RECENT_QUERY_MAX_AGE_YEARS if max_age_years is None else max_age_years
    if years <= 0:
        return ranked
    today = today or date.today()
    cutoff = today - timedelta(days=int(years * 365.25))
    fresh: list[tuple[Document, float]] = []
    undated: list[tuple[Document, float]] = []
    for doc, score in ranked:
        meeting = document_meeting_date(doc)
        if meeting is None:
            undated.append((doc, score))
        elif meeting >= cutoff:
            fresh.append((doc, score))
    if fresh:
        return sort_hits_newest_first(fresh)
    if undated:
        return undated
    return []


def _is_board_doc(doc: Document) -> bool:
    """True for meeting/board chunks (chunking.py) — the only source lacking a
    source_type tag; supplemental sources (articles/pages/events/PDFs) all
    carry one (see sources/documents.py)."""
    return not doc.metadata.get("source_type")


# Per source-type "bucket" (board records vs. supplemental sources), how many
# top-fused-rank candidates are guaranteed a shot at the reranker, and how
# many top-reranked-and-boosted results are reserved in the final cap — so a
# broad topic where one source type dominates the raw ranking (e.g. several
# comprehensive news articles vs. scattered single-purpose board contracts)
# doesn't crowd the other source type out entirely when both are relevant.
_MIN_BUCKET_CANDIDATES = 3
_MIN_BUCKET_RESULTS = 2


def _reserve_by_bucket(
    items: list[tuple[Document, float]], min_per_bucket: int, cap: int
) -> list[tuple[Document, float]]:
    board = [t for t in items if _is_board_doc(t[0])]
    supplemental = [t for t in items if not _is_board_doc(t[0])]
    reserved = board[:min_per_bucket] + supplemental[:min_per_bucket]
    reserved_ids = {id(d) for d, _ in reserved}
    fill = [t for t in items if id(t[0]) not in reserved_ids][: max(cap - len(reserved), 0)]
    combined = reserved + fill
    combined.sort(key=lambda t: -t[1])
    return combined[:cap]


# How many objectively-newest-dated documents to reserve a candidate slot for
# when a query wants "recent" items.
_RECENCY_TOPUP = 4


def _recent_topup(store: DataStore, n: int) -> list[Document]:
    """The n most-recently-dated documents in the corpus, split evenly across
    source-type buckets (board records vs. supplemental).

    Dense/BM25 candidate selection is purely semantic/keyword — it can't tell
    this month's "agenda approved" boilerplate from five years ago's, since
    that line is nearly identical every time. Without this, "recent" queries
    can end up reranking whichever arbitrary instances happened to embed
    closest to the query text, which has no relationship to which ones are
    actually recent. This guarantees the true newest records at least reach
    the reranker/recency-boost stage, which already know how to prefer them.

    Split by bucket because supplemental sources (news articles) publish far
    more often than board meetings happen — a global newest-N would be filled
    entirely by articles, leaving board records with no recency reservation
    at all.
    """
    half = max(n // 2, 1)
    board_docs = [d for d in store.documents if _is_board_doc(d)]
    supplemental_docs = [d for d in store.documents if not _is_board_doc(d)]
    out: list[Document] = []
    for bucket in (board_docs, supplemental_docs):
        dated = [(d, document_meeting_date(d)) for d in bucket]
        dated_only = [(d, dt) for d, dt in dated if dt is not None]
        dated_only.sort(key=lambda t: t[1], reverse=True)
        out.extend(d for d, _ in dated_only[:half])
    return out


def _reserve_recent(
    ranked: list[tuple[Document, float]], pool: list[tuple[Document, float]], n: int
) -> list[tuple[Document, float]]:
    """Union the n most-recently-dated items from the full reranked list into
    pool (split per source-type bucket — see _recent_topup), bypassing
    SCORE_THRESHOLD for just those.

    Without this, a genuinely-recent-but-only-tangentially-on-topic candidate
    (reserved by _recent_topup specifically for its date) can score too low
    on the cross-encoder to survive the relevance filter — even though the
    downstream recency logic (apply_recency_boost / prefer_recent_hits) would
    correctly recognize it as fresh once given the chance.
    """
    half = max(n // 2, 1)

    def _top_recent(pred) -> list[tuple[Document, float]]:
        dated = sorted(
            (t for t in ranked if pred(t[0]) and document_meeting_date(t[0]) is not None),
            key=lambda t: document_meeting_date(t[0]),
            reverse=True,
        )
        return dated[:half]

    extra = _top_recent(_is_board_doc) + _top_recent(lambda d: not _is_board_doc(d))
    pool_ids = {id(d) for d, _ in pool}
    return pool + [t for t in extra if id(t[0]) not in pool_ids]


def hybrid_retrieve(
    store: DataStore,
    query: str,
    *,
    intent_query: str | None = None,
) -> list[tuple[Document, float]]:
    """Dense FAISS + BM25 via RRF, then optional cross-encoder rerank + recency."""
    if store.vectorstore is None or store.bm25 is None:
        return []

    intent = intent_query if intent_query is not None else query
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
    fused_docs = [doc_map[doc_id] for doc_id, _ in fused if doc_id in doc_map]

    # Guarantee both source-type buckets reach the reranker, instead of
    # candidates being whichever RERANK_CANDIDATES docs the raw fusion rank
    # happened to favor (which can be 100% one source type — see
    # _reserve_by_bucket for why that's also re-checked after reranking).
    board_bucket = [d for d in fused_docs if _is_board_doc(d)][:_MIN_BUCKET_CANDIDATES]
    supplemental_bucket = [d for d in fused_docs if not _is_board_doc(d)][:_MIN_BUCKET_CANDIDATES]
    reserved_docs = board_bucket + supplemental_bucket

    # "Recent" queries need a genuine recency top-up — see _recent_topup.
    if query_wants_recent(intent):
        reserved_ids_so_far = {id(d) for d in reserved_docs}
        reserved_docs += [
            d for d in _recent_topup(store, _RECENCY_TOPUP) if id(d) not in reserved_ids_so_far
        ]

    reserved_ids = {id(d) for d in reserved_docs}
    fill_docs = [d for d in fused_docs if id(d) not in reserved_ids][
        : max(RERANK_CANDIDATES - len(reserved_docs), 0)
    ]
    candidates = reserved_docs + fill_docs

    if not candidates:
        return apply_recency_boost(
            [(d, float(s)) for d, s in dense_hits[:RERANK_K]],
            query,
            intent_query=intent,
        )

    if not ENABLE_RERANKER:
        ranked = [(d, 1.0 - (i * 0.05)) for i, d in enumerate(candidates[: max(RERANK_K * 2, RERANK_K)])]
        if query_wants_recent(intent):
            ranked = _reserve_recent(ranked, ranked, _RECENCY_TOPUP)
        boosted = apply_recency_boost(ranked, query, intent_query=intent)
        return _reserve_by_bucket(boosted, _MIN_BUCKET_RESULTS, RERANK_K)

    reranker = get_reranker()
    pairs = [(query, (d.page_content or "")[:_MAX_CHARS_PER_DOC]) for d in candidates]
    scores = reranker.predict(pairs)
    # Always coerce to Python float — numpy.float32 is not JSON-serializable.
    ranked = [(d, float(s)) for d, s in sorted(zip(candidates, scores), key=lambda x: -float(x[1]))]
    filtered = [(d, s) for d, s in ranked if s >= SCORE_THRESHOLD]
    pool = filtered or ranked
    if query_wants_recent(intent):
        # Bypass SCORE_THRESHOLD for the objectively most-recent candidates —
        # see _reserve_recent.
        pool = _reserve_recent(ranked, pool, _RECENCY_TOPUP)
    boosted = apply_recency_boost(pool, query, intent_query=intent)
    return _reserve_by_bucket(boosted, _MIN_BUCKET_RESULTS, RERANK_K)


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
    # Present newest sources first in the prompt context so the model cites
    # recent articles/meetings before older ones.
    ordered = sort_hits_newest_first(list(hits), by_date_primary=True)
    return "\n\n--- RECORD ---\n\n".join(d.page_content for d, _ in ordered)


def best_score(hits: list[tuple[Document, float]]) -> float:
    return float(max((float(s) for _, s in hits), default=0.0))


def hits_meta(hits: list[tuple[Document, float]]) -> dict[str, Any]:
    return {
        "retrieved": len(hits),
        "best_score": round(best_score(hits), 4),
        "chunk_ids": [d.metadata.get("chunk_id") for d, _ in hits],
        "meeting_dates": [d.metadata.get("meeting_date") for d, _ in hits],
    }
