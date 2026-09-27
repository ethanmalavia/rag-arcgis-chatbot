"""Corrective RAG path: hybrid retrieval, grading, rewrite, single Claude call for generation.

Cards are built deterministically from retrieved-document metadata (never
LLM-authored) — see build_cards(). The LLM only writes the free-form prose
answer; it never re-extracts title/id/location/status/date/url itself, so a
source's type (board record vs. news article/page/event) can't be lost or
miscategorized, and the answer isn't capped to a fixed bullet count.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from langchain_core.documents import Document

from config import ANSWER_MAX_TOKENS, ANSWER_TEMPERATURE, CRAG_MAX_ITERS, SCORE_THRESHOLD
from models import ChatResponse, ProjectOut, RouteKind
from prompt_loader import load_prompt
from config import RECENT_QUERY_MAX_AGE_YEARS
from retrieval import (
    best_score,
    format_records_for_llm,
    hits_meta,
    hybrid_retrieve,
    merge_records_for_llm,
    query_wants_recent,
    scope_hits_to_project,
)
from stale_sources import parse_source_date
from store import DataStore
from structured_path import _clip_at_sentence, _row_to_project

logger = logging.getLogger(__name__)

_STRAY_FENCE_RE = re.compile(r"```(?:json)?[\s\S]*?```", re.IGNORECASE)
_FENCE_STRIP_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
_HEADER_LINE_RE = re.compile(r"^(?:DATE|SOURCE_TYPE|TITLE|SEARCH|TRUE_URL|venue|location|category):", re.IGNORECASE)

_ANSWER_SYSTEM_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "answer_system.md"
_VALID_STATUSES = {"Approved", "Denied", "Continued", "No decision recorded"}
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


def rewrite_search_query(question: str) -> str:
    """Always-on pre-retrieval rewrite: expand a short/bare question into a
    clearer search query (e.g. "wawa" -> "Wawa development proposals,
    approvals, construction, and status in Estero"). Distinct from
    rewrite_query() above, which only fires as a CRAG retry after a failed
    retrieval grade — this one runs once, up front, for every question.
    Falls back to the original question unchanged if the model call fails or
    looks unusable, so a rewrite hiccup never blocks retrieval.
    """
    from config import ENABLE_QUERY_REWRITE, QUERY_REWRITE_MAX_TOKENS, QUERY_REWRITE_TEMPERATURE

    q = question.strip()
    if not ENABLE_QUERY_REWRITE or not q:
        return question
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
        cleaned = " ".join((result.text or "").strip().split())
        if not cleaned or len(cleaned) < 3:
            return question
        logger.info("Query rewrite: %r -> %r", q[:80], cleaned[:160])
        return cleaned
    except Exception as exc:  # noqa: BLE001 — never block retrieval on this
        logger.warning("Query rewrite unavailable (%s); using original question", exc)
        return question


def retrieve_with_crag(
    store: DataStore, question: str
) -> tuple[str, dict[str, Any], list[tuple[Document, float]]]:
    search_query = rewrite_search_query(question)
    query = search_query
    meta: dict[str, Any] = {"crag_iters": 0, "rewrites": [], "search_query": search_query}
    hits: list[tuple[Document, float]] = []
    for i in range(CRAG_MAX_ITERS):
        meta["crag_iters"] = i + 1
        # Recency intent always follows the original citizen question.
        hits = hybrid_retrieve(store, query, intent_query=question)
        verdict = grade_context(hits)
        meta["last_verdict"] = verdict
        if verdict == "correct":
            break
        if verdict in {"incorrect", "ambiguous"} and i < CRAG_MAX_ITERS - 1:
            query = rewrite_query(question)
            meta["rewrites"].append(query)
    scoped = scope_hits_to_project(store, hits)
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
    store: DataStore, hits: list[tuple[Document, float]], used_ids: set[str] | None = None
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
    """
    seen_articles: set[tuple[str, str]] = set()
    articles: list[ProjectOut] = []
    board_by_id: dict[str, ProjectOut] = {}
    for doc, _score in hits:
        md = doc.metadata
        source_type = md.get("source_type")
        if source_type:
            record_id = md.get("record_id") or ""
            url = md.get("url") or md.get("document_url") or ""
            if not url:
                continue
            if used_ids is not None and record_id not in used_ids:
                continue
            key = (source_type, record_id or url)
            if key in seen_articles:
                continue
            seen_articles.add(key)
            articles.append(
                ProjectOut(
                    title=(md.get("title") or "").strip(),
                    id=record_id,
                    location=md.get("location") or md.get("venue") or "",
                    summary=_clip_at_sentence(_strip_header_lines(doc.page_content), 220),
                    status="",
                    date=md.get("publish_date") or md.get("date") or "",
                    article_url=url,
                    source_type=source_type,
                    category=md.get("category") or "",
                )
            )
        else:
            row_index = md.get("row_index")
            if row_index is None:
                continue
            # Most PZDB project decisions have an ApplicationID; most Village
            # Council agenda items (consent agenda, financial reports, generic
            # business) do not — those still need a stable per-row dedupe key
            # so they aren't silently dropped.
            app_id = str(md.get("application_id") or "").strip()
            dedupe_key = app_id or f"row-{row_index}"
            if used_ids is not None and dedupe_key not in used_ids:
                continue
            try:
                row = store.dataframe.iloc[int(row_index)].to_dict()
            except (IndexError, ValueError, TypeError):
                continue
            card = _row_to_project(row)
            card.source_type = "board_record"
            existing = board_by_id.get(dedupe_key)
            if existing is None or (parse_source_date(card.date) or date.min) >= (
                parse_source_date(existing.date) or date.min
            ):
                board_by_id[dedupe_key] = card
    return (list(board_by_id.values()) + articles)[:8]


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

    system = _load_answer_system_prompt()
    valid_ids = set(record_ids or [])
    user = f"Resident question: {question}\n\nContext records:\n{context}"

    try:
        result = claude_client.generate(
            system=system, user=user, max_tokens=ANSWER_MAX_TOKENS, temperature=ANSWER_TEMPERATURE
        )
    except claude_client.ClaudeError as exc:
        logger.error("Claude answer call failed for %r: %s", question[:80], exc)
        return StructuredAnswer(answer_markdown=_FRIENDLY_LLM_ERROR, source_type="general", used_fallback=True)

    parsed = _parse_structured_json(result.text, valid_ids)
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
        return StructuredAnswer(answer_markdown=_FRIENDLY_LLM_ERROR, source_type="general", used_fallback=True)

    parsed = _parse_structured_json(result2.text, valid_ids)
    if parsed is not None:
        return parsed

    logger.warning("Structured answer JSON invalid on retry for %r — falling back to plain text", question[:80])
    fallback_text = finalize_prose(_STRAY_FENCE_RE.sub("", result2.text or result.text).strip())
    return StructuredAnswer(
        answer_markdown=fallback_text or "I don't have records on that.",
        source_type="general",
        used_fallback=True,
    )


def answer_rag(store: DataStore, question: str) -> ChatResponse:
    t0 = time.perf_counter()
    context, crag_meta, hits = retrieve_with_crag(store, question)
    crag_meta["retrieve_ms"] = round((time.perf_counter() - t0) * 1000)
    t1 = time.perf_counter()
    structured = generate_answer(question, context, crag_meta.get("retrieved_record_ids"))
    crag_meta["generate_ms"] = round((time.perf_counter() - t1) * 1000)
    crag_meta["used_fallback_answer"] = structured.used_fallback
    crag_meta["used_record_ids"] = structured.used_record_ids
    crag_meta["source_type"] = structured.source_type
    # Only cards for records the LLM actually cited (used_record_ids) — never
    # show a card the answer doesn't reference. Sort/recency-cutoff only, NOT
    # filter_projects_for_query's entity-overlap drop (built for LLM-invented
    # project lists) — these cards come from hits the retrieval/rerank/CRAG
    # pipeline already vetted, and the LLM has already curated which ones to
    # use above.
    cards = filter_projects_for_recency(
        question, build_cards(store, hits, used_ids=set(structured.used_record_ids))
    )
    crag_meta.update(
        {
            "llm_provider": "anthropic",
            "llm_model": _model_name(),
            "prompt_variant": _variant_name(),
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
        meta=crag_meta,
    )
