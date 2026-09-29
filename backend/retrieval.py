"""Hybrid dense+sparse retrieval with RRF fusion, cross-encoder reranking, and recency."""
from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from typing import Any

from langchain_core.documents import Document
from sentence_transformers import CrossEncoder
from torch import nn

from config import (
    DENSE_K,
    ENABLE_ONNX_RERANKER,
    ENABLE_PROJECT_SCOPE,
    ENABLE_RECENCY_BOOST,
    ENABLE_RERANKER,
    MIN_RERANK_ABS,
    MIN_RERANK_RELATIVE,
    MIN_RERANK_SCORE,
    PROJECT_SCOPE_CAP,
    PROJECT_SCOPE_MIN_SUPPORT,
    RECENCY_BOOST,
    RECENCY_HALF_LIFE_DAYS,
    RECENT_QUERY_BOOST,
    RECENT_QUERY_MAX_AGE_YEARS,
    RERANK_CANDIDATES,
    RERANKER_MODEL,
    RERANK_K,
    SPARSE_K,
)
from onnx_reranker import OnnxCrossEncoder, load_onnx_reranker
from schema_aliases import row_value
from store import DataStore, _tokenize

_reranker: CrossEncoder | OnnxCrossEncoder | None = None
_reranker_lock = threading.Lock()

# Phrasings of one question (topic_queries + the LLM rewrite) are retrieved
# concurrently: a single cross-encoder call doesn't saturate the CPU, so two
# overlapping calls finish ~30% sooner than back-to-back ones, with identical
# scores. Shared across requests; torch releases the GIL while scoring.
RETRIEVAL_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="retrieve")
# Small reranker batches pad each batch only to its own longest pair instead
# of the longest of all ~20 candidates — ~15% faster, same scores.
_RERANK_BATCH_SIZE = 4
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
    r"recent(?:ly)?|latest|newest|newly|freshly|lately|nowadays|"
    r"just\s+(?:approved|passed|announced|voted|adopted|opened|added)|"
    r"these\s+days|right\s+now|as\s+of\s+(?:now|today)|"
    r"this\s+(?:year|month|week)|last\s+(?:year|week)|"
    r"past\s+(?:year|months?|weeks?|few\s+(?:years?|months?|weeks?))|"
    r"last\s+(?:few\s+)?(?:years?|months?|weeks?)|"
    r"new(?:er)?|current(?:ly)?"
    r")\b",
    re.IGNORECASE,
)


# "history and latest info on X" wants the whole timeline, not just the newest
# slice — history intent overrides the recent-only hard cutoff.
_HISTORY_QUERY_RE = re.compile(
    r"\b(history|historical|background|timeline|backstory|over\s+the\s+years|"
    r"origins?|originally|evolution)\b",
    re.IGNORECASE,
)


def query_wants_history(query: str) -> bool:
    return bool(_HISTORY_QUERY_RE.search(query or ""))


def query_wants_recent(query: str) -> bool:
    """True when the question asks for recent/new/latest (and names no year).

    False when the question also asks for history: the resident wants the full
    timeline ending in the latest state, so old records must not be dropped.
    """
    q = (query or "").strip()
    if not q or _YEAR_RE.search(q) or query_wants_history(q):
        return False
    return bool(_RECENT_QUERY_RE.search(q))


# Conversational scaffolding that says nothing about the topic. Left in the
# retrieval query it drags in whatever merely shares those words — e.g.
# "history and latest information Coconut Point" matched generic history/news
# pages instead of the named place. "what is happening" is deliberately kept:
# article titles like "What Development is Happening along East Corkscrew Road"
# are exactly the sources that answer such questions.
_FOCUS_FILLER_RES = [
    re.compile(r"\b(?:can|could|would)\s+you\b", re.IGNORECASE),
    re.compile(r"\b(?:please|give\s+me|show\s+me|tell\s+me\s+about|tell\s+me|get\s+me)\b", re.IGNORECASE),
    re.compile(r"\b(?:the\s+)?(?:history|background|timeline|backstory)\b(?:\s+of)?", re.IGNORECASE),
    re.compile(
        r"\b(?:the\s+)?(?:latest|newest|recent|current)\s+"
        r"(?:information|info|news|updates?|developments?|status)?\b(?:\s+(?:on|about|for|regarding|of))?",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:information|info|details|updates?|news)\s+(?:on|about|regarding)\b", re.IGNORECASE),
]
_FOCUS_EDGE_RE = re.compile(r"^(?:and|the|of|on|about|for|to)\s+|\s+(?:and|the|of|on|about|for|to)$", re.IGNORECASE)


_QUESTION_STOPWORDS = frozenset(
    {"what", "are", "was", "were", "the", "and", "any", "for", "with", "about", "there",
     "this", "that", "how", "why", "who", "when", "where", "does", "did", "has", "have",
     "happening", "going", "new"}
)
_HAPPENING_FILLER_RE = re.compile(
    r"\bwhat(?:'?s|\s+is|\s+are)?\s+(?:currently\s+|now\s+)?"
    r"(?:happening|going\s+on|new)\b(?:\s+(?:to|at|on|with|in|near|around|about|for))?",
    re.IGNORECASE,
)


def focus_query(question: str, *, strip_happening: bool = False) -> str:
    """The topic of *question* with conversational filler removed.

    Used only for candidate retrieval/reranking; recency/history intent and the
    LLM prompt still use the original question. Falls back to the original when
    stripping leaves no content word (e.g. "What are the recent developments?").

    strip_happening also removes "what is happening at/on/to" — see
    topic_queries for why that variant is searched alongside the default one.
    """
    q = (question or "").strip()
    text = q
    filler = _FOCUS_FILLER_RES + ([_HAPPENING_FILLER_RE] if strip_happening else [])
    for rx in filler:
        text = rx.sub(" ", text)
    text = re.sub(r"[?!.,;:]+", " ", text)
    text = " ".join(text.split())
    prev = None
    while prev != text:
        prev = text
        text = _FOCUS_EDGE_RE.sub("", text).strip()
    content = [
        t for t in re.findall(r"[a-z0-9]+", text.lower())
        if len(t) >= 3 and t not in _QUESTION_STOPWORDS
    ]
    if not content:
        return q
    return text


def topic_queries(question: str) -> list[str]:
    """Distinct retrieval queries for *question*: with and without "what is happening".

    Keeping the phrase surfaces articles titled "What Development is Happening
    along East Corkscrew Road"; dropping it surfaces records about the named
    place itself ("what is happening at Estero Parkway" otherwise returns only
    the East Corkscrew "What's happening" article). Neither variant wins for
    every question, so both are searched and merged by score.
    """
    seen: list[str] = []
    for q in (focus_query(question), focus_query(question, strip_happening=True)):
        if q and q not in seen:
            seen.append(q)
    return seen


def get_reranker() -> CrossEncoder | OnnxCrossEncoder:
    if _reranker is None:
        # The startup warm-up and the first requests (plus their concurrent
        # phrasings) can all arrive here at once — load exactly one copy.
        with _reranker_lock:
            if _reranker is None:
                _load_reranker()
    return _reranker


def _load_reranker() -> None:
    global _reranker
    print(f"Loading reranker {RERANKER_MODEL}…")
    if ENABLE_ONNX_RERANKER:
        # Same model and scores on ONNX Runtime (sigmoid applied there too) —
        # see onnx_reranker. None means fall back to PyTorch below.
        onnx_model = load_onnx_reranker()
        if onnx_model is not None:
            print("Reranker backend: onnxruntime")
            _reranker = onnx_model
            return
    # Always score on a 0-1 sigmoid scale: MIN_RERANK_SCORE, the recency
    # boost and CRAG grading all assume it. bge-reranker-base already
    # does, but ms-marco-MiniLM ships an Identity activation and would
    # return raw logits (~-10..+10), which silently swamps the recency term.
    # SDPA is PyTorch's fused attention kernel: same math as the eager
    # implementation (score drift < 1e-6, identical rankings on the eval
    # set), just faster on CPU.
    _reranker = CrossEncoder(
        RERANKER_MODEL,
        default_activation_function=nn.Sigmoid(),
        automodel_args={"attn_implementation": "sdpa"},
    )
    print("Reranker backend: pytorch")


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


# Max chunks from one source record (article / board row) in the final hits, so
# a single long article can't fill every slot and crowd out other records.
_MAX_CHUNKS_PER_RECORD = 3
_MAX_CHUNKS_PER_RECORD_HISTORY = 2
# History questions need a wider window to span the timeline.
_HISTORY_EXTRA_RESULTS = 4


def _record_key(doc: Document) -> str:
    md = doc.metadata
    if md.get("source_type"):
        return f"{md['source_type']}:{md.get('record_id') or md.get('url') or md.get('chunk_id')}"
    return f"board:{md.get('row_index', md.get('chunk_id'))}"


def _cap_per_record(
    items: list[tuple[Document, float]], per_record: int
) -> list[tuple[Document, float]]:
    """Keep the best *per_record* chunks of each source record (input order = rank)."""
    counts: dict[str, int] = {}
    out: list[tuple[Document, float]] = []
    for item in items:
        key = _record_key(item[0])
        counts[key] = counts.get(key, 0) + 1
        if counts[key] <= per_record:
            out.append(item)
    return out


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
    ranked: list[tuple[Document, float]],
    pool: list[tuple[Document, float]],
    n: int,
    query: str | None = None,
) -> list[tuple[Document, float]]:
    """Union the n most-recently-dated items from the full reranked list into
    pool (split per source-type bucket — see _recent_topup), bypassing
    MIN_RERANK_SCORE for just those.

    Without this, a genuinely-recent-but-only-tangentially-on-topic candidate
    (reserved by _recent_topup specifically for its date) can score too low
    on the cross-encoder to survive the relevance filter — even though the
    downstream recency logic (apply_recency_boost / prefer_recent_hits) would
    correctly recognize it as fresh once given the chance.

    When *query* is given, a reserved item must share at least one topic term
    with it. Otherwise a specific question ("latest on Coconut Point") gets the
    corpus-wide newest documents (unrelated I-75 / personnel-policy items) as
    its only "fresh" hits, and prefer_recent_hits then discards every genuinely
    relevant but older record in their favour.
    """
    half = max(n // 2, 1)
    topic_terms = (set(_tokenize(focus_query(query))) - _GENERIC_QUERY_TERMS) if query else set()

    def _on_topic(doc: Document) -> bool:
        return not topic_terms or bool(topic_terms & set(_tokenize(doc.page_content)))

    def _top_recent(pred) -> list[tuple[Document, float]]:
        dated = sorted(
            (
                t
                for t in ranked
                if pred(t[0]) and document_meeting_date(t[0]) is not None and _on_topic(t[0])
            ),
            key=lambda t: document_meeting_date(t[0]),
            reverse=True,
        )
        return dated[:half]

    extra = _top_recent(_is_board_doc) + _top_recent(lambda d: not _is_board_doc(d))
    pool_ids = {id(d) for d, _ in pool}
    return pool + [t for t in extra if id(t[0]) not in pool_ids]


def _finalize(
    boosted: list[tuple[Document, float]], intent: str
) -> list[tuple[Document, float]]:
    history = query_wants_history(intent)
    capped = _cap_per_record(
        boosted, _MAX_CHUNKS_PER_RECORD_HISTORY if history else _MAX_CHUNKS_PER_RECORD
    )
    cap = RERANK_K + (_HISTORY_EXTRA_RESULTS if history else 0)
    return _reserve_by_bucket(capped, _MIN_BUCKET_RESULTS, cap)


_DEV_APPROVAL_RE = re.compile(r"\b(?:approved?|approvals?|approving|green-?lit|okay(?:ed)?)\b", re.IGNORECASE)
_DEV_TOPIC_RE = re.compile(
    r"\b(?:developments?|developers?|projects?|"
    r"new\s+(?:businesses|stores|buildings|homes|restaurants))\b",
    re.IGNORECASE,
)
_DEV_CATEGORIES = frozenset({"commercial_mixed_use_development", "residential_development"})
_DEV_APP_TYPES = frozenset({"dos", "dci", "ldo", "add", "cpa"})
_DEV_EXCLUDED_TYPES = frozenset({"resolution", "ordinance"})
_DEV_EXCLUDED_FACTS = frozenset({"consent_agenda", "administrative", "contract_approval"})
_DEV_APPROVAL_LIMIT = 8


# Words that carry no named subject. If anything else is left after removing
# them ("was the Wawa project approved?"), the question is about a specific
# thing and normal retrieval — not the generic newest-approvals list — applies.
_DEV_GENERIC_TERMS = frozenset(
    {"tell", "about", "show", "give", "list", "what", "which", "any", "all", "some", "were", "was",
     "are", "the", "and", "for", "have", "has", "been", "got", "get", "there", "estero", "village",
     "recent", "recently", "latest", "newest", "newly", "new", "lately", "just", "current",
     "currently", "past", "last", "this", "year", "years", "month", "months", "week", "weeks",
     "now", "these", "days", "today", "few", "approved", "approve", "approval", "approvals",
     "approving", "okayed", "greenlit", "development", "developments", "developer", "developers",
     "project", "projects",
     "businesses", "stores", "buildings", "homes", "restaurants", "with", "that", "does", "did",
     "can", "you", "please", "town", "area", "local", "board", "council", "planning", "zoning"}
)


# Question/recency scaffolding that isn't a topic — used so the recent-docs
# top-up doesn't treat "approved", "newly", "happened" … as subject words.
_GENERIC_QUERY_TERMS = _DEV_GENERIC_TERMS | _QUESTION_STOPWORDS | frozenset(
    {"happened", "happen", "happens", "anything", "news", "update", "updates", "info",
     "information", "upcoming", "most", "fresh", "freshly", "than", "into", "from"}
)


def query_wants_development_approvals(query: str) -> bool:
    """Generic 'recently approved developments' / 'recent developments' questions.

    Needs a development word plus an approval or recency word, and no other
    subject (a road, project name, company…): those use normal retrieval.
    """
    q = query or ""
    if not _DEV_TOPIC_RE.search(q):
        return False
    if not (_DEV_APPROVAL_RE.search(q) or _RECENT_QUERY_RE.search(q)):
        return False
    leftovers = [
        t for t in re.findall(r"[a-z0-9]+", _YEAR_RE.sub(" ", q).lower())
        if t not in _DEV_GENERIC_TERMS and len(t) > 2
    ]
    return not leftovers


def _development_approval_docs(
    store: DataStore,
    n: int = _DEV_APPROVAL_LIMIT,
    year: int | None = None,
    approved_only: bool = True,
) -> list[Document]:
    """Canonical 'meta' chunks of the n newest development items (approved only
    unless the question didn't ask about approvals — "recent developments").

    A development item is a land-use application (DOS/DCI/LDO/ADD/CPA) or a
    commercial/residential-category item that isn't a resolution, ordinance,
    consent-agenda, administrative or contract row.
    """
    df = store.dataframe
    needed = {"Status", "MeetingDate", "ApplicationType", "LandUseCategory", "FactCategory"}
    if df is None or df.empty or not needed <= set(df.columns):
        return []
    status = df["Status"].astype(str).str.lower()
    app_type = df["ApplicationType"].astype(str).str.lower()
    category = df["LandUseCategory"].astype(str)
    fact = df["FactCategory"].astype(str)
    is_dev = app_type.isin(_DEV_APP_TYPES) | (
        category.isin(_DEV_CATEGORIES)
        & ~app_type.isin(_DEV_EXCLUDED_TYPES)
        & ~fact.isin(_DEV_EXCLUDED_FACTS)
    )
    keep = is_dev & (status.str.contains("approv", na=False) if approved_only else True)
    if year is not None:
        keep &= df["MeetingDate"].astype(str).str.startswith(str(year))
    rows = df[keep].sort_values("MeetingDate", ascending=False)
    wanted = [int(i) for i in rows.index[:n]]
    meta_by_row = {
        d.metadata.get("row_index"): d for d in store.documents if d.metadata.get("chunk_type") == "meta"
    }
    return [meta_by_row[i] for i in wanted if i in meta_by_row]


def rerank_floor(best: float) -> float:
    """Minimum rerank score a candidate needs to reach the answer model.

    MIN_RERANK_SCORE when the best candidate clears it (bge-reranker-base
    scores a clear match ~0.98). Otherwise a fraction of the best score, never
    below MIN_RERANK_ABS: ms-marco-MiniLM scores long resident questions very
    low even for the right document (0.004 vs ~0.0000 for noise), and a fixed
    0.25 floor would leave those questions with no records at all.
    """
    if best >= MIN_RERANK_SCORE:
        return MIN_RERANK_SCORE
    return max(MIN_RERANK_ABS, best * MIN_RERANK_RELATIVE)


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
        : max(RERANK_CANDIDATES + (8 if query_wants_history(intent) else 0) - len(reserved_docs), 0)
    ]
    candidates = reserved_docs + fill_docs

    # "recently approved developments": the indexed chunk text has no notion of
    # "development", while "approved" matches every agenda-approval/resolution
    # row — so select the newest approved development items from the data itself.
    year_m = _YEAR_RE.search(intent or "")
    dev_docs = (
        _development_approval_docs(
            store,
            year=int(year_m.group(1)) if year_m else None,
            approved_only=bool(_DEV_APPROVAL_RE.search(intent or "")),
        )
        if query_wants_development_approvals(intent)
        else []
    )
    dev_ids = {id(d) for d in dev_docs}
    if dev_docs:
        candidates += [d for d in dev_docs if id(d) not in {id(c) for c in candidates}]

    if not candidates:
        return apply_recency_boost(
            [(d, float(s)) for d, s in dense_hits[:RERANK_K]],
            query,
            intent_query=intent,
        )

    if not ENABLE_RERANKER:
        ranked = [(d, 1.0 - (i * 0.05)) for i, d in enumerate(candidates[: max(RERANK_K * 2, RERANK_K)])]
        if dev_docs:
            ranked = [(d, s) for d, s in ranked if not _is_board_doc(d) or id(d) in dev_ids]
            ranked += [(d, 1.0) for d in dev_docs if id(d) not in {id(r[0]) for r in ranked}]
        if query_wants_recent(intent) and not dev_docs:
            ranked = _reserve_recent(ranked, ranked, _RECENCY_TOPUP, query=intent)
        boosted = apply_recency_boost(ranked, query, intent_query=intent)
        return _finalize(boosted, intent)

    reranker = get_reranker()
    pairs = [(query, (d.page_content or "")[:_MAX_CHARS_PER_DOC]) for d in candidates]
    scores = reranker.predict(pairs, batch_size=_RERANK_BATCH_SIZE, show_progress_bar=False)
    # Always coerce to Python float — numpy.float32 is not JSON-serializable.
    ranked = [(d, float(s)) for d, s in sorted(zip(candidates, scores), key=lambda x: -float(x[1]))]
    floor = rerank_floor(ranked[0][1] if ranked else 0.0)
    filtered = [(d, s) for d, s in ranked if s >= floor]
    # Hard floor — deliberately NOT "filtered or ranked". Falling back to the
    # unfiltered list when nothing clears the bar is exactly how an unrelated
    # doc (e.g. a rail-trail article for a "wawa" question) used to slip back
    # in: _reserve_by_bucket below guarantees a couple of results per
    # source-type bucket, and if `pool` still contained 0.0-scored docs, that
    # guarantee would happily promote one of them just to fill the quota.
    # The floor is relative to the best candidate (see rerank_floor), so a
    # long question MiniLM scores low overall keeps its best matches.
    pool = filtered
    if dev_docs:
        # Curated approvals bypass the floor; unrelated board rows (agenda
        # approval, personnel policy…) that only matched "approved" are dropped.
        curated = [(d, max(s, floor)) for d, s in ranked if id(d) in dev_ids]
        pool = curated + [(d, s) for d, s in filtered if not _is_board_doc(d)]
    if query_wants_recent(intent) and not dev_docs:
        # Recency intent is the one other exception: bypass the floor for the
        # most-recent on-topic candidates — see _reserve_recent. The topic check
        # reads the citizen's question, not `query`: an LLM-expanded search
        # query adds broad words ("construction", "status") that would let
        # corpus-wide newest docs through.
        pool = _reserve_recent(ranked, pool, _RECENCY_TOPUP, query=intent)
    boosted = apply_recency_boost(pool, query, intent_query=intent)
    return _finalize(boosted, intent)


def hybrid_retrieve_multi(
    store: DataStore,
    queries: list[str],
    *,
    intent_query: str | None = None,
) -> list[tuple[Document, float]]:
    """hybrid_retrieve over several query phrasings, merged by best score per chunk."""
    if len(queries) == 1:
        return hybrid_retrieve(store, queries[0], intent_query=intent_query)
    futures = [
        RETRIEVAL_POOL.submit(hybrid_retrieve, store, q, intent_query=intent_query) for q in queries
    ]
    return merge_phrasing_hits(
        [f.result() for f in futures], intent_query if intent_query is not None else queries[0]
    )


def merge_phrasing_hits(
    per_query: list[list[tuple[Document, float]]], intent: str
) -> list[tuple[Document, float]]:
    """Merge several phrasings' hybrid_retrieve results by best score per chunk."""
    if len(per_query) == 1:
        return per_query[0]
    best: dict[str, tuple[Document, float]] = {}
    for hits in per_query:
        for doc, score in hits:
            key = doc.metadata.get("chunk_id") or str(id(doc))
            if key not in best or score > best[key][1]:
                best[key] = (doc, score)
    merged = sorted(best.values(), key=lambda t: -t[1])
    return _finalize(merged, intent)


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


def _record_type(source_type: str) -> str:
    """Collapse the finer source_type vocabulary (website_article /
    website_page / event / document) into the label the answer model sees."""
    if not source_type:
        return "board record"
    if source_type == "website_article":
        return "article"
    if source_type == "event":
        return "event"
    return "document"


def record_citation_id(doc: Document, row: dict[str, Any] | None = None) -> str:
    """The one ID a retrieved record is known by — in the LLM context, in its
    used_record_ids/timeline/related citations, and as the dedupe key the
    answer's cards are filtered on (rag_path.build_cards). Keeping all three on
    this single helper is what makes "cite [X]" line up with "card X".

    Supplemental sources: record_id (e.g. "post-1234"), else URL. Board rows:
    ApplicationID, else "row-<row_index>" (most Village Council agenda items
    have no ApplicationID).
    """
    md = doc.metadata
    if md.get("source_type"):
        return str(md.get("record_id") or md.get("url") or md.get("document_url") or "").strip()
    app_id = row_value(row, "application_id").strip() if row else ""
    app_id = app_id or str(md.get("application_id") or "").strip()
    return app_id or f"row-{md.get('row_index')}"


def is_internal_record_id(record_id: str) -> bool:
    """IDs that mean nothing to a resident and are never shown: synthetic
    row-N keys, and the full URLs legacy EsteroToday articles are keyed by
    (sources/__init__.py backfill). Their cards still render and link out."""
    rid = str(record_id or "")
    return rid.startswith("row-") or rid.startswith(("http://", "https://"))


_CONTEXT_HEADER_LINE_RE = re.compile(
    r"^(?:DATE|SOURCE_TYPE|TITLE|SEARCH|TRUE_URL|venue|location|category):", re.IGNORECASE
)
# Per-record excerpt budget for the answer model. Articles carry the detail
# (lane counts, dollar amounts, schedules) the answer needs, so they get more
# room than a board row's summary field.
_EXCERPT_CHARS_BOARD = 900
_EXCERPT_CHARS_SUPPLEMENTAL = 1600


def _clean_chunk_text(text: str) -> str:
    lines = [ln for ln in (text or "").splitlines() if not _CONTEXT_HEADER_LINE_RE.match(ln.strip())]
    return " ".join(" ".join(lines).split())


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return (cut[: end + 1] if end > limit * 0.6 else cut.rstrip()) + " …"


def merge_records_for_llm(store: DataStore, hits: list[tuple[Document, float]]) -> list[dict[str, Any]]:
    """Merge hit chunks belonging to the same record into one entry, so a
    record isn't sent to the answer model twice (e.g. its 'meta' chunk and
    its 'summary' chunk both surviving rerank). IDs come from
    record_citation_id, the same key build_cards filters on.

    Each entry carries the text the answer is grounded in — a board row's
    Summary, or the retrieved chunk text of an article/page/event (several
    chunks of one article are joined in rank order). Title/date alone are not
    enough to answer from. Keeps the best rerank score across a record's
    chunks, for eval logging.
    """
    board_by_id: dict[str, dict[str, Any]] = {}
    supplemental_by_id: dict[str, dict[str, Any]] = {}

    for doc, score in hits:
        md = doc.metadata
        source_type = md.get("source_type")
        if source_type:
            rid = record_citation_id(doc)
            if not rid:
                continue
            text = _clean_chunk_text(doc.page_content)
            entry = supplemental_by_id.get(rid)
            if entry is not None:
                entry["score"] = max(entry["score"], float(score))
                if text and text not in entry["_chunks"]:
                    entry["_chunks"].append(text)
                continue
            supplemental_by_id[rid] = {
                "id": rid,
                "type": _record_type(source_type),
                "title": str(md.get("title") or "").strip(),
                "date": str(md.get("publish_date") or md.get("date") or "").strip(),
                "location": str(md.get("location") or md.get("venue") or "").strip(),
                "score": float(score),
                "_chunks": [text] if text else [],
            }
            continue

        row_index = md.get("row_index")
        if row_index is None:
            continue
        try:
            row = store.dataframe.iloc[int(row_index)].to_dict()
        except (IndexError, ValueError, TypeError):
            continue
        rid = record_citation_id(doc, row)
        this_date = row_value(row, "meeting_date")
        existing = board_by_id.get(rid)
        best = max(float(score), existing["score"]) if existing else float(score)
        # Same dedupe rule as build_cards: keep the most-recently-dated meeting
        # row for this record.
        if existing is not None and (existing["date"] or "") >= this_date:
            existing["score"] = best
            continue
        board_by_id[rid] = {
            "id": rid,
            "type": "board record",
            "title": row_value(row, "project_name"),
            "date": this_date,
            "board": row_value(row, "board"),
            "location": row_value(row, "location"),
            "action": row_value(row, "action_taken"),
            "outcome": row_value(row, "outcome"),
            "status": row_value(row, "status"),
            "text": _clip(" ".join(row_value(row, "summary").split()), _EXCERPT_CHARS_BOARD),
            "score": best,
        }

    for entry in supplemental_by_id.values():
        entry["text"] = _clip(" … ".join(entry.pop("_chunks")), _EXCERPT_CHARS_SUPPLEMENTAL)
    return list(board_by_id.values()) + list(supplemental_by_id.values())


def format_records_for_llm(records: list[dict[str, Any]]) -> str:
    """One block per record, newest first (so "latest" questions read the
    current state before older history):

        [ID] type | title | date
        Board: … | Location: … | Action: … | Outcome: …
        <summary / article excerpt>
    """
    if not records:
        return "No relevant records found in the dataset."

    ordered = sorted(records, key=lambda r: r.get("date") or "", reverse=True)
    blocks = []
    for r in ordered:
        head = " | ".join(x for x in (r.get("type", ""), r.get("title", ""), r.get("date") or "undated") if x)
        facts = " | ".join(
            f"{label}: {r[key]}"
            for key, label in (
                ("board", "Board"),
                ("location", "Location"),
                ("action", "Action"),
                ("outcome", "Outcome"),
                ("status", "Status"),
            )
            if r.get(key) and not (key == "outcome" and r.get(key) == r.get("action"))
        )
        lines = [f"[{r.get('id', '')}] {head}"]
        if facts:
            lines.append(facts)
        if r.get("text"):
            lines.append(r["text"])
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


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
