"""Corrective RAG path: hybrid retrieval, grading, rewrite, single Claude call for generation.

Cards are built deterministically from retrieved-document metadata (never
LLM-authored) — see build_cards(). The LLM only writes the free-form prose
answer; it never re-extracts title/id/location/status/date/url itself, so a
source's type (board record vs. news article/page/event) can't be lost or
miscategorized, and the answer isn't capped to a fixed bullet count.
"""
from __future__ import annotations

import copy
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from collections.abc import Iterator
from typing import Any

from langchain_core.documents import Document

from config import ANSWER_MAX_TOKENS, ANSWER_TEMPERATURE, CRAG_MAX_ITERS, SCORE_THRESHOLD
from models import ChatResponse, ProjectOut, RouteKind
from prompt_loader import load_prompt
from config import RECENT_QUERY_MAX_AGE_YEARS
from retrieval import (
    RETRIEVAL_POOL,
    best_score,
    format_records_for_llm,
    hits_meta,
    hybrid_retrieve,
    hybrid_retrieve_multi,
    is_internal_record_id,
    merge_phrasing_hits,
    merge_records_for_llm,
    query_wants_development_approvals,
    query_wants_recent,
    record_citation_id,
    scope_hits_to_project,
    topic_queries,
)
from stale_sources import parse_source_date
from store import DataStore
from structured_path import _clip_at_sentence, _row_to_project

logger = logging.getLogger(__name__)

_STRAY_FENCE_RE = re.compile(r"```(?:json)?[\s\S]*?```", re.IGNORECASE)
_FENCE_STRIP_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
_HEADER_LINE_RE = re.compile(r"^(?:DATE|SOURCE_TYPE|TITLE|SEARCH|TRUE_URL|venue|location|category):", re.IGNORECASE)

_ANSWER_SYSTEM_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "answer_system.md"
_VALID_STATUSES = {"Approved", "Denied", "Continued", "No decision recorded", "Update"}
_VALID_SOURCE_TYPES = {"records", "mixed", "general"}


@lru_cache(maxsize=1)
def _load_answer_system_prompt() -> str:
    """Flat, non-variant prompt file (unlike prompts/<variant>/answer.txt,
    loaded via prompt_loader) — this is the sole structured-JSON answer
    prompt, not something PROMPT_VARIANT switches between."""
    return _ANSWER_SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")


@dataclass
class StructuredAnswer:
    answer_markdown: str = ""
    timeline: list[dict[str, str]] = field(default_factory=list)
    related: list[dict[str, str]] = field(default_factory=list)
    used_record_ids: list[str] = field(default_factory=list)
    follow_ups: list[str] = field(default_factory=list)
    source_type: str = "records"
    used_fallback: bool = False
    # The Claude call itself failed (answer_markdown is the friendly error).
    llm_error: bool = False


def _prompt(name: str) -> str:
    import config as cfg

    return load_prompt(name, cfg.PROMPT_VARIANT)


def _variant_name() -> str:
    import config as cfg

    return cfg.PROMPT_VARIANT


def _model_name() -> str:
    import config as cfg

    return cfg.LLM_MODEL


def _strip_header_lines(text: str) -> str:
    """Drop the DATE:/SOURCE_TYPE:/TITLE:/SEARCH:/TRUE_URL: header block and
    venue:/location:/category: body lines baked into supplemental-source chunk
    text (see sources/documents.py) — those are retrieval aids, not prose."""
    lines = [ln for ln in text.splitlines() if not _HEADER_LINE_RE.match(ln.strip())]
    text = "\n".join(lines).strip()
    # A mid-corpus chunk often opens mid-sentence (a leading ". Foo bar...").
    # If it doesn't start with an uppercase letter/quote, drop the fragment
    # before the first sentence boundary so card blurbs read cleanly.
    if text and not (text[0].isupper() or text[0] in "\"'“"):
        m = re.search(r"[.!?]\s+", text[:120])
        if m:
            text = text[m.end():]
    return text.strip()


def finalize_prose(text: str) -> str:
    """Trim quotes/whitespace and drop a trailing incomplete fragment."""
    text = (text or "").strip().strip('"').strip("'").strip()
    if not text:
        return text
    if text[-1] in ".!?":
        return text
    sentence_ends = [m.end() - 1 for m in re.finditer(r"[.!?](?=\s|$)", text)]
    if sentence_ends and sentence_ends[-1] >= 20:
        return text[: sentence_ends[-1] + 1].strip()
    if len(text.split()) >= 6:
        return text.rstrip(",;:- ") + "."
    return text


def filter_projects_for_recency(question: str, projects: list[ProjectOut]) -> list[ProjectOut]:
    """Prefer newest project/article cards; harden when user asks for recent.

    Always sorts dated cards newest-first so citations lead with recent sources.
    When the question asks for recent/new/latest, also drop cards older than
    RECENT_QUERY_MAX_AGE_YEARS (fresh → undated → empty).
    """
    if not projects:
        return projects

    def _newest_first(items: list[ProjectOut]) -> list[ProjectOut]:
        return sorted(
            items,
            key=lambda p: parse_source_date(p.date) or date.min,
            reverse=True,
        )

    if not query_wants_recent(question):
        return _newest_first(list(projects))

    years = RECENT_QUERY_MAX_AGE_YEARS
    if years <= 0:
        return _newest_first(list(projects))
    cutoff = date.today() - timedelta(days=int(years * 365.25))
    kept: list[ProjectOut] = []
    undated: list[ProjectOut] = []
    for p in projects:
        d = parse_source_date(p.date)
        if d is None:
            undated.append(p)
        elif d >= cutoff:
            kept.append(p)
    if kept:
        result = _newest_first(kept)
    elif undated:
        result = undated
    else:
        result = []
    if result != projects:
        logger.info(
            "filter_projects_for_recency kept %s/%s for %r",
            len(result),
            len(projects),
            question[:80],
        )
    return result


def grade_context(hits: list[tuple[Document, float]]) -> str:
    if not hits:
        return "incorrect"
    score = best_score(hits)
    if score < SCORE_THRESHOLD * 0.5:
        return "incorrect"
    if score < SCORE_THRESHOLD:
        return "ambiguous"
    return "correct"


def rewrite_query(question: str) -> str:
    """Expand a weak query for a CRAG retry without injecting bare years.

    Prefer Claude Haiku when configured (cheap rewrite job). Otherwise use
    the deterministic rule-based expansion. Bare years are avoided so they
    cannot trip query_wants_recent if intent were derived from the rewrite.
    """
    haiku = _haiku_rewrite_query(question)
    if haiku:
        return haiku
    base = f"{question.strip()} Estero Florida planning zoning design board"
    if query_wants_recent(question):
        return f"{base} recent planning meetings last two years"
    return f"{base} newest articles recent coverage"


def _haiku_rewrite_query(question: str) -> str | None:
    """Optional Haiku job: rewrite a weak RAG query toward recent Estero sources."""
    from config import ENABLE_HAIKU_REWRITE

    if not ENABLE_HAIKU_REWRITE:
        return None
    try:
        import claude_client

        result = claude_client.generate(
            system=(
                "Rewrite the citizen question into one short English search query "
                "for Village of Estero planning/zoning records and EsteroToday articles. "
                "Prefer terms that surface the newest coverage. "
                "Do not invent years. Reply with the query only — no quotes or preamble."
            ),
            user=question.strip(),
            max_tokens=120,
            temperature=0,
        )
        cleaned = " ".join((result.text or "").strip().split())
        if not cleaned or len(cleaned) < 8:
            return None
        # Guard against year injection that would disable recent-mode intent.
        if re.search(r"\b20\d{2}\b", cleaned) and not re.search(r"\b20\d{2}\b", question):
            cleaned = re.sub(r"\b20\d{2}\b", "", cleaned)
            cleaned = " ".join(cleaned.split())
        logger.info("Haiku CRAG rewrite: %r -> %r", question[:80], cleaned[:120])
        return cleaned
    except Exception as exc:  # noqa: BLE001 — fall back to rules
        logger.warning("Haiku rewrite unavailable (%s); using rule-based rewrite", exc)
        return None


_CONTENT_WORD_RE = re.compile(r"[a-z0-9][a-z0-9'-]*", re.IGNORECASE)
_YEAR_TOKEN_RE = re.compile(r"\b20\d{2}\b")


def _is_bare_query(question: str) -> bool:
    """Short keyword-style input ("wawa", "sandy lane", "corkscrew road 2024")
    — the case the pre-retrieval rewrite exists for. Full questions already
    retrieve well on their own topic words (retrieval.topic_queries), and
    rewriting them costs an extra LLM round-trip and risks drifting intent."""
    from config import QUERY_REWRITE_MAX_WORDS

    return len(_CONTENT_WORD_RE.findall(question or "")) <= QUERY_REWRITE_MAX_WORDS


def rewrite_search_query(question: str) -> str | None:
    """Pre-retrieval rewrite: expand a short/bare question into a clearer
    search query (e.g. "wawa" -> "Wawa development proposals, approvals,
    construction, and status in Estero"). Distinct from rewrite_query()
    above, which only fires as a CRAG retry after a failed retrieval grade.

    Returns None when not applicable (disabled, not a bare query) or when the
    model call fails / looks unusable. The rewrite is searched *alongside*
    the literal question, never instead of it, so an application ID or
    street name can't be lost by a paraphrase.
    """
    from config import ENABLE_QUERY_REWRITE, QUERY_REWRITE_MAX_TOKENS, QUERY_REWRITE_TEMPERATURE

    q = question.strip()
    if not ENABLE_QUERY_REWRITE or not q or not _is_bare_query(q):
        return None
    try:
        import claude_client

        result = claude_client.generate(
            system=(
                "Rewrite the resident's question into one clear, complete search "
                "query for Village of Estero planning/zoning records and "
                "EsteroToday articles. Expand short or bare inputs into a full "
                "query — e.g. 'wawa' -> 'Wawa development proposals, approvals, "
                "construction, and status in Estero'. Keep the resident's "
                "original intent; don't invent specifics (dates, statuses) "
                "that weren't asked about. Reply with the query only — no "
                "quotes or preamble."
            ),
            user=q,
            max_tokens=QUERY_REWRITE_MAX_TOKENS,
            temperature=QUERY_REWRITE_TEMPERATURE,
        )
        cleaned = " ".join((result.text or "").strip().split()).strip("\"'")
        if not cleaned or len(cleaned) < 3:
            return None
        # A year the resident never typed would flip recency/historical intent
        # downstream and bias retrieval toward that year.
        if _YEAR_TOKEN_RE.search(cleaned) and not _YEAR_TOKEN_RE.search(q):
            cleaned = " ".join(_YEAR_TOKEN_RE.sub("", cleaned).split())
        logger.info("Query rewrite: %r -> %r", q[:80], cleaned[:160])
        return cleaned
    except Exception as exc:  # noqa: BLE001 — never block retrieval on this
        logger.warning("Query rewrite unavailable (%s); using original question", exc)
        return None


_RETRIEVAL_CACHE_SIZE = 256
_retrieval_cache_lock = threading.Lock()


def retrieve_with_crag(
    store: DataStore, question: str
) -> tuple[str, dict[str, Any], list[tuple[Document, float]]]:
    """Retrieval for *question*, reusing the result for a repeated question.

    Retrieval is a pure function of (index, question, today's date — the
    recency cutoffs read it), so a repeat — the prompt-help templates make
    identical questions common — skips the ~3s rerank with identical hits.
    The cache lives on the DataStore, so a rebuilt index starts empty.
    """
    cache = getattr(store, "retrieval_cache", None)
    if cache is None:
        return _retrieve_with_crag(store, question)
    key = (" ".join(question.split()), date.today().isoformat())
    with _retrieval_cache_lock:
        cached = cache.get(key)
        if cached is not None:
            cache.move_to_end(key)
    if cached is not None:
        context, meta, hits = cached
        return context, {**copy.deepcopy(meta), "retrieval_cached": True}, list(hits)
    context, meta, hits = _retrieve_with_crag(store, question)
    with _retrieval_cache_lock:
        cache[key] = (context, copy.deepcopy(meta), list(hits))
        while len(cache) > _RETRIEVAL_CACHE_SIZE:
            cache.popitem(last=False)
    return context, meta, hits


def _retrieve_with_crag(
    store: DataStore, question: str
) -> tuple[str, dict[str, Any], list[tuple[Document, float]]]:
    # Retrieve on the topic only; recency/history intent still reads `question`.
    # A bare keyword query is also searched in its LLM-expanded form; results
    # from every phrasing are merged by best score per chunk.
    queries = topic_queries(question)
    # The LLM rewrite (~1s) runs alongside retrieval of the literal phrasings
    # instead of before it; its own retrieval starts as soon as it returns.
    literal = [
        RETRIEVAL_POOL.submit(hybrid_retrieve, store, q, intent_query=question) for q in queries
    ]
    search_query = rewrite_search_query(question)
    rewritten = (
        RETRIEVAL_POOL.submit(hybrid_retrieve, store, search_query, intent_query=question)
        if search_query and search_query not in queries
        else None
    )
    if rewritten is not None:
        queries.append(search_query)
    meta: dict[str, Any] = {"crag_iters": 0, "rewrites": [], "queries": list(queries)}
    if search_query:
        meta["search_query"] = search_query
    hits: list[tuple[Document, float]] = []
    for i in range(CRAG_MAX_ITERS):
        meta["crag_iters"] = i + 1
        # Recency intent always follows the original citizen question.
        if i == 0:
            first_pass = [f.result() for f in literal + ([rewritten] if rewritten else [])]
            hits = merge_phrasing_hits(first_pass, question)
        else:
            hits = hybrid_retrieve_multi(store, queries, intent_query=question)
        verdict = grade_context(hits)
        meta["last_verdict"] = verdict
        if verdict == "correct":
            break
        if verdict in {"incorrect", "ambiguous"} and i < CRAG_MAX_ITERS - 1:
            queries = [rewrite_query(question)]
            meta["rewrites"].append(queries[0])
    # A generic list of developments spans many projects; narrowing to the one
    # project two hits happen to share would hide the rest.
    scoped = hits if query_wants_development_approvals(question) else scope_hits_to_project(store, hits)
    if len(scoped) != len(hits):
        meta["project_scoped"] = len(scoped)
    meta.update(hits_meta(scoped))
    records = merge_records_for_llm(store, scoped)
    meta["record_count"] = len(records)
    # "Retrieved" (offered to the LLM) vs. "used" (what it actually cited,
    # set later in answer_rag from the LLM's used_record_ids) are logged
    # separately — see scripts/eval_answers.py.
    meta["retrieved_record_ids"] = [r["id"] for r in records if r.get("id")]
    meta["rerank_scores"] = {r["id"]: round(r.get("score", 0.0), 4) for r in records if r.get("id")}
    return format_records_for_llm(records), meta, scoped


def build_cards(
    store: DataStore,
    hits: list[tuple[Document, float]],
    used_ids: set[str] | None = None,
    cite_order: list[str] | None = None,
) -> list[ProjectOut]:
    """Cards built deterministically from retrieved-document metadata.

    Supplemental sources (articles/pages/events/PDFs) already carry a
    source_type plus title/date/url/location on doc.metadata (see
    sources/documents.py) — used directly. Meeting/board chunks only carry
    application_id/row_index, so those are joined back to store.dataframe and
    built the same way the STRUCTURED route already does (_row_to_project).

    The same application_id can appear as a separate dataframe row per
    meeting it came before (e.g. a design review, then a later approval) —
    so board records are deduped by keeping the row with the latest
    meeting_date per application_id, not just the first one retrieval
    happened to rank highest.

    When used_ids is given, only records the LLM actually cited in
    used_record_ids become cards — everything else it saw but didn't use
    stays hidden (the answer's "related" list covers loosely-relevant ones
    instead). Pass None to keep the old "every retrieved record" behavior
    (e.g. for callers that don't have an LLM answer to filter against).
    Records are keyed by retrieval.record_citation_id — the same ID the LLM
    saw and cited — so a citation always matches its card.

    When cite_order is given (the LLM's used_record_ids), cards follow the
    order the answer cites them in, before the 8-card cap is applied.
    """
    articles: dict[str, ProjectOut] = {}
    board_by_id: dict[str, ProjectOut] = {}
    for doc, _score in hits:
        md = doc.metadata
        source_type = md.get("source_type")
        if source_type:
            url = md.get("url") or md.get("document_url") or ""
            if not url:
                continue
            key = record_citation_id(doc)
            if used_ids is not None and key not in used_ids:
                continue
            if key in articles:
                continue
            articles[key] = ProjectOut(
                title=(md.get("title") or "").strip(),
                id=key,
                location=md.get("location") or md.get("venue") or "",
                summary=_clip_at_sentence(_strip_header_lines(doc.page_content), 220),
                status="",
                date=md.get("publish_date") or md.get("date") or "",
                article_url=url,
                source_type=source_type,
                category=md.get("category") or "",
            )
        else:
            row_index = md.get("row_index")
            if row_index is None:
                continue
            try:
                row = store.dataframe.iloc[int(row_index)].to_dict()
            except (IndexError, ValueError, TypeError):
                continue
            # Most PZDB project decisions have an ApplicationID; most Village
            # Council agenda items (consent agenda, financial reports, generic
            # business) do not — those still need a stable per-row dedupe key
            # (row-N) so they aren't silently dropped.
            dedupe_key = record_citation_id(doc, row)
            if used_ids is not None and dedupe_key not in used_ids:
                continue
            card = _row_to_project(row)
            card.source_type = "board_record"
            existing = board_by_id.get(dedupe_key)
            if existing is None or (parse_source_date(card.date) or date.min) >= (
                parse_source_date(existing.date) or date.min
            ):
                board_by_id[dedupe_key] = card
    keyed = list(board_by_id.items()) + list(articles.items())
    if cite_order:
        rank = {rid: i for i, rid in enumerate(cite_order)}
        keyed.sort(key=lambda kv: rank.get(kv[0], len(rank)))
    return [card for _key, card in keyed][:8]


def _strip_fence(text: str) -> str:
    return _FENCE_STRIP_RE.sub("", text.strip()).strip()


def _parse_structured_json(raw: str, valid_ids: set[str]) -> StructuredAnswer | None:
    """Parse+validate one JSON completion. Returns None on any failure so the
    caller can retry or fall back — never raises."""
    try:
        data = json.loads(_strip_fence(raw))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    answer_markdown = data.get("answer_markdown")
    timeline = data.get("timeline")
    related = data.get("related")
    used_record_ids = data.get("used_record_ids")
    follow_ups = data.get("follow_ups")
    source_type = data.get("source_type")
    if not isinstance(answer_markdown, str) or not answer_markdown.strip():
        return None
    if not all(isinstance(x, list) for x in (timeline, related, used_record_ids, follow_ups)):
        return None

    def _clean_timeline(items: list) -> list[dict[str, str]]:
        out = []
        for it in items:
            if not isinstance(it, dict):
                continue
            rid = str(it.get("record_id") or "").strip()
            if valid_ids and rid and rid not in valid_ids:
                continue  # never let an invented record ID through
            status = str(it.get("status") or "").strip()
            if status not in _VALID_STATUSES:
                status = "No decision recorded"
            out.append({
                "date": str(it.get("date") or "").strip(),
                "event": str(it.get("event") or "").strip(),
                "status": status,
                "record_id": rid,
            })
        return out

    def _clean_related(items: list) -> list[dict[str, str]]:
        out = []
        for it in items:
            if not isinstance(it, dict):
                continue
            rid = str(it.get("record_id") or "").strip()
            if valid_ids and rid and rid not in valid_ids:
                continue
            out.append({"record_id": rid, "one_line": str(it.get("one_line") or "").strip()})
        return out

    clean_ids = [str(x).strip() for x in used_record_ids if str(x).strip()]
    if valid_ids:
        clean_ids = [rid for rid in clean_ids if rid in valid_ids]

    clean_source_type = str(source_type or "").strip().lower()
    if clean_source_type not in _VALID_SOURCE_TYPES:
        # Fall back to a sensible default rather than reject the whole
        # response over one bad enum value — infer from whether it actually
        # cited anything.
        clean_source_type = "records" if clean_ids else "general"

    return StructuredAnswer(
        answer_markdown=finalize_prose(_STRAY_FENCE_RE.sub("", answer_markdown).strip()) or answer_markdown.strip(),
        timeline=_clean_timeline(timeline),
        related=_clean_related(related),
        used_record_ids=clean_ids,
        follow_ups=[str(x).strip() for x in follow_ups if str(x).strip()][:3],
        source_type=clean_source_type,
    )


_ANSWER_FIELD_RE = re.compile(r'"answer_markdown"\s*:\s*"((?:[^"\\]|\\.)*)', re.DOTALL)


def _salvage_answer_markdown(raw: str) -> str:
    """Pull answer_markdown out of JSON that failed to parse — usually a reply
    cut off at max_tokens partway through the timeline — so the resident gets
    the prose instead of a raw ```json dump."""
    m = _ANSWER_FIELD_RE.search(raw or "")
    if not m:
        return ""
    body = m.group(1)
    try:
        text = json.loads(f'"{body}"')
    except json.JSONDecodeError:
        # Cut mid-escape: drop the dangling backslash sequence and retry.
        try:
            text = json.loads(f'"{body.rsplit(chr(92), 1)[0]}"')
        except json.JSONDecodeError:
            return ""
    return finalize_prose(text.strip())


_FRIENDLY_LLM_ERROR = (
    "Sorry, I'm having trouble reaching the AI service right now. Please try again in a moment."
)


def generate_answer(question: str, context: str, record_ids: list[str] | None = None) -> StructuredAnswer:
    """One Claude Haiku call via claude_client — structured JSON grounded in
    context. claude_client is imported lazily (not at module top) so
    importing rag_path never hard-fails when ANTHROPIC_API_KEY isn't set yet
    — matching this module's existing lazy-import convention.

    On invalid/unparseable JSON: retries once with a stricter reminder, then
    falls back to a plain-text answer (raw text as answer_markdown, source_type
    "general", everything else empty) rather than failing the request
    outright. On a Claude API failure that survives claude_client's own
    timeout+retry, returns a friendly in-chat error message instead of
    raising — a resident should never see a stack trace.
    """
    import claude_client

    system, user, valid_ids = _answer_request(question, context, record_ids)
    try:
        result = claude_client.generate(
            system=system, user=user, max_tokens=ANSWER_MAX_TOKENS, temperature=ANSWER_TEMPERATURE
        )
    except claude_client.ClaudeError as exc:
        logger.error("Claude answer call failed for %r: %s", question[:80], exc)
        return _llm_error_answer()
    return _finish_answer(question, system, user, valid_ids, result.text)


def generate_answer_stream(
    question: str, context: str, record_ids: list[str] | None = None
) -> Iterator[str | StructuredAnswer]:
    """generate_answer, streamed: yields answer_markdown text deltas while
    Claude writes the JSON, then the final StructuredAnswer as the last item.

    The deltas are a live preview only — the final answer (validated JSON,
    finalize_prose, hidden-ID stripping) replaces it on "done". A reply that
    fails to parse takes the same retry/fallback path as generate_answer.
    """
    import claude_client

    system, user, valid_ids = _answer_request(question, context, record_ids)
    raw_parts: list[str] = []
    preview = _AnswerPreview()
    try:
        for chunk in claude_client.stream_generate(
            system=system, user=user, max_tokens=ANSWER_MAX_TOKENS, temperature=ANSWER_TEMPERATURE
        ):
            raw_parts.append(chunk)
            delta = preview.feed(chunk)
            if delta:
                yield delta
    except claude_client.ClaudeError as exc:
        logger.error("Claude answer stream failed for %r: %s", question[:80], exc)
        yield _llm_error_answer()
        return
    yield _finish_answer(question, system, user, valid_ids, "".join(raw_parts))


class _AnswerPreview:
    """Incrementally decodes the answer_markdown string out of a JSON reply
    that is still being written, returning only the newly-stable text.

    Holds back an unterminated escape sequence and any "[…" citation that
    hasn't closed yet, so internal IDs ([row-12], article URLs) are stripped
    before they ever reach the screen rather than flashing and vanishing.
    """

    _START_RE = re.compile(r'"answer_markdown"\s*:\s*"')

    def __init__(self) -> None:
        self._raw = ""
        self._start: int | None = None
        self._closed = False
        self._sent = ""

    def feed(self, chunk: str) -> str:
        if self._closed:
            return ""
        self._raw += chunk
        if self._start is None:
            m = self._START_RE.search(self._raw)
            if not m:
                return ""
            self._start = m.end()
        body, self._closed = self._scan(self._raw[self._start:])
        try:
            text = json.loads(f'"{body}"')
        except json.JSONDecodeError:
            return ""
        if not self._closed:
            open_bracket = text.rfind("[")
            if open_bracket > text.rfind("]"):
                text = text[:open_bracket]
        text = _strip_hidden_citations(text)
        if not self._closed:
            # Citation tidy-up rewrites whitespace before punctuation, so
            # never send trailing whitespace until something follows it.
            text = text.rstrip()
        if len(text) <= len(self._sent):
            return ""
        # Normally text extends what was sent. If a tidy-up touched an
        # already-sent character, resync by length — the preview may be one
        # character off until "done" swaps in the final answer, but it never
        # stalls or repeats text.
        delta = text[len(self._sent):]
        self._sent = text
        return delta

    @staticmethod
    def _scan(s: str) -> tuple[str, bool]:
        """(decodable JSON-string body so far, whether the string closed)."""
        i = 0
        while i < len(s):
            c = s[i]
            if c == "\\":
                need = 6 if s[i + 1 : i + 2] == "u" else 2
                if i + need > len(s):
                    return s[:i], False
                i += need
                continue
            if c == '"':
                return s[:i], True
            i += 1
        return s, False


def _answer_request(
    question: str, context: str, record_ids: list[str] | None
) -> tuple[str, str, set[str]]:
    system = _load_answer_system_prompt()
    user = f"Resident question: {question}\n\nContext records:\n{context}"
    return system, user, set(record_ids or [])


def _llm_error_answer() -> StructuredAnswer:
    return StructuredAnswer(
        answer_markdown=_FRIENDLY_LLM_ERROR, source_type="general", used_fallback=True, llm_error=True
    )


def _finish_answer(
    question: str, system: str, user: str, valid_ids: set[str], raw: str
) -> StructuredAnswer:
    """Parse the first reply; on invalid JSON retry once, then fall back to
    plain text (see generate_answer)."""
    import claude_client

    parsed = _parse_structured_json(raw, valid_ids)
    if parsed is not None:
        return parsed

    logger.warning("Structured answer JSON invalid on first attempt for %r — retrying once", question[:80])
    retry_user = (
        f"{user}\n\n"
        "Your previous reply was not valid JSON matching the required schema. "
        "Reply again with ONLY the JSON object described in the system prompt — "
        "no prose, no code fence, no explanation."
    )
    try:
        result2 = claude_client.generate(
            system=system, user=retry_user, max_tokens=ANSWER_MAX_TOKENS, temperature=ANSWER_TEMPERATURE
        )
    except claude_client.ClaudeError as exc:
        logger.error("Claude answer retry failed for %r: %s", question[:80], exc)
        return _llm_error_answer()

    parsed = _parse_structured_json(result2.text, valid_ids)
    if parsed is not None:
        return parsed

    logger.warning("Structured answer JSON invalid on retry for %r — falling back to plain text", question[:80])
    raw = result2.text or raw
    fallback_text = _salvage_answer_markdown(raw) or finalize_prose(_STRAY_FENCE_RE.sub("", raw).strip())
    return StructuredAnswer(
        answer_markdown=fallback_text or "I don't have records on that.",
        # The plain-text answer was still written from the retrieved records —
        # only badge it "general" when there were none to write from.
        source_type="records" if valid_ids else "general",
        used_fallback=True,
    )


# One citation or a run of them: "[A]", "[A], [B]", "[A][B]".
_CITATION_GROUP_RE = re.compile(r"[ \t]*\[[^\[\]\n]{1,300}\](?:\s*,?\s*\[[^\[\]\n]{1,300}\])*")
_CITATION_ID_RE = re.compile(r"\[([^\[\]\n]{1,300})\]")


def _strip_hidden_citations(text: str) -> str:
    def _group(m: re.Match) -> str:
        ids = _CITATION_ID_RE.findall(m.group(0))
        kept = [rid for rid in ids if not is_internal_record_id(rid.strip())]
        if len(kept) == len(ids):
            return m.group(0)
        return (" " + ", ".join(f"[{rid}]" for rid in kept)) if kept else ""

    text = _CITATION_GROUP_RE.sub(_group, text)
    # Tidy what a removed citation leaves behind: "2024**,." / "(, " / " ,".
    text = re.sub(r",+(\s*[.;:!?)])", r"\1", text)
    text = re.sub(r"[ \t]+([,.;:!?)])", r"\1", text)
    return text


def _hide_internal_ids(structured: StructuredAnswer) -> None:
    """Remove unreadable record IDs from everything a resident sees.

    Board rows without an ApplicationID are keyed "row-N", and legacy articles
    by their URL (see retrieval.record_citation_id) — both can be cited and
    carded, but the ID itself is meaningless on screen. used_record_ids keeps
    them: build_cards needs it.
    """
    structured.answer_markdown = _strip_hidden_citations(structured.answer_markdown)
    for item in structured.timeline + structured.related:
        if is_internal_record_id(item.get("record_id", "")):
            item["record_id"] = ""


def cards_for_answer(
    store: DataStore,
    question: str,
    hits: list[tuple[Document, float]],
    structured: StructuredAnswer,
) -> list[ProjectOut]:
    """Cards for the records the answer actually cites, in citation order.

    No recency cutoff here: the LLM already chose which records to cite, and
    dropping an older one it relied on (e.g. the original approval in a
    "latest status" answer) would leave a citation pointing at nothing.
    Retrieval itself still applies the recent-only cutoff for recency
    questions (retrieval.prefer_recent_hits).

    Fallbacks: an unparseable-JSON answer carries no used_record_ids, so all
    retrieved cards are shown the old way; an LLM outage or a "general"
    answer shows none.
    """
    if structured.llm_error or structured.source_type == "general":
        return []
    if structured.used_fallback:
        return filter_projects_for_recency(question, build_cards(store, hits))
    return build_cards(
        store,
        hits,
        used_ids=set(structured.used_record_ids),
        cite_order=structured.used_record_ids,
    )


def build_rag_response(
    store: DataStore,
    question: str,
    hits: list[tuple[Document, float]],
    structured: StructuredAnswer,
    meta: dict[str, Any],
) -> ChatResponse:
    """ChatResponse for a structured answer — shared by answer_rag and the
    streaming path (orchestrator.stream_answer) so both display the same."""
    cards = cards_for_answer(store, question, hits, structured)
    _hide_internal_ids(structured)
    meta.update(
        {
            "llm_provider": "anthropic",
            "llm_model": _model_name(),
            "prompt_variant": _variant_name(),
            "used_fallback_answer": structured.used_fallback,
            "used_record_ids": structured.used_record_ids,
            "source_type": structured.source_type,
        }
    )
    return ChatResponse(
        summary=structured.answer_markdown,
        projects=cards,
        answer=structured.answer_markdown,
        timeline=structured.timeline,
        related=structured.related,
        used_record_ids=structured.used_record_ids,
        follow_ups=structured.follow_ups,
        source_type=structured.source_type,
        route=RouteKind.RAG.value,
        meta=meta,
    )


def answer_rag(store: DataStore, question: str) -> ChatResponse:
    t0 = time.perf_counter()
    context, crag_meta, hits = retrieve_with_crag(store, question)
    crag_meta["retrieve_ms"] = round((time.perf_counter() - t0) * 1000)
    t1 = time.perf_counter()
    structured = generate_answer(question, context, crag_meta.get("retrieved_record_ids"))
    crag_meta["generate_ms"] = round((time.perf_counter() - t1) * 1000)
    return build_rag_response(store, question, hits, structured, crag_meta)
