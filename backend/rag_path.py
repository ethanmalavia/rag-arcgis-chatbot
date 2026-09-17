"""Corrective RAG path: hybrid retrieval, grading, rewrite, single Claude call for generation.

Cards are built deterministically from retrieved-document metadata (never
LLM-authored) — see build_cards(). The LLM only writes the free-form prose
answer; it never re-extracts title/id/location/status/date/url itself, so a
source's type (board record vs. news article/page/event) can't be lost or
miscategorized, and the answer isn't capped to a fixed bullet count.
"""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import date, timedelta
from typing import Any

from langchain_core.documents import Document

from config import CRAG_MAX_ITERS, SCORE_THRESHOLD
from models import ChatResponse, ProjectOut, RouteKind
from prompt_loader import load_prompt
from config import RECENT_QUERY_MAX_AGE_YEARS
from retrieval import (
    best_score,
    format_docs,
    hits_meta,
    hybrid_retrieve,
    query_wants_recent,
    scope_hits_to_project,
)
from stale_sources import parse_source_date
from store import DataStore
from structured_path import _clip_at_sentence, _row_to_project

logger = logging.getLogger(__name__)

_STRAY_FENCE_RE = re.compile(r"```(?:json)?[\s\S]*?```", re.IGNORECASE)
_HEADER_LINE_RE = re.compile(r"^(?:DATE|SOURCE_TYPE|TITLE|SEARCH|TRUE_URL|venue|location|category):", re.IGNORECASE)


def _prompt(name: str) -> str:
    import config as cfg

    return load_prompt(name, cfg.PROMPT_VARIANT)


def _variant_name() -> str:
    import config as cfg

    return cfg.PROMPT_VARIANT


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
    from config import ENABLE_HAIKU_REWRITE, HAIKU_REWRITE_MODEL

    if not ENABLE_HAIKU_REWRITE:
        return None
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return None
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=HAIKU_REWRITE_MODEL,
            max_tokens=120,
            temperature=0,
            system=(
                "Rewrite the citizen question into one short English search query "
                "for Village of Estero planning/zoning records and EsteroToday articles. "
                "Prefer terms that surface the newest coverage. "
                "Do not invent years. Reply with the query only — no quotes or preamble."
            ),
            messages=[{"role": "user", "content": question.strip()}],
        )
        text = ""
        for block in msg.content:
            if getattr(block, "type", None) == "text":
                text += block.text
        cleaned = " ".join((text or "").strip().split())
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


def retrieve_with_crag(
    store: DataStore, question: str
) -> tuple[str, dict[str, Any], list[tuple[Document, float]]]:
    query = question
    meta: dict[str, Any] = {"crag_iters": 0, "rewrites": []}
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
    return format_docs(scoped), meta, scoped


def build_cards(store: DataStore, hits: list[tuple[Document, float]]) -> list[ProjectOut]:
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


def generate_answer(question: str, context: str) -> str:
    """One Claude call via llm_provider — free-form prose grounded in context.

    llm_provider is imported lazily (not at module top) because it constructs
    and validates its LLM client at import time, raising if ANTHROPIC_API_KEY
    is unset — matching this module's existing lazy-import convention so
    importing rag_path never hard-fails when no key is configured yet.
    """
    import llm_provider

    system = _prompt("answer")
    user = f"Resident question: {question}\n\nContext blocks:\n{context}"
    result = llm_provider.generate(system=system, user=user, max_tokens=1200)
    prose = result.text.strip()
    prose = _STRAY_FENCE_RE.sub("", prose).strip()
    return finalize_prose(prose) or "I don't have records on that."


def answer_rag(store: DataStore, question: str) -> ChatResponse:
    t0 = time.perf_counter()
    context, crag_meta, hits = retrieve_with_crag(store, question)
    crag_meta["retrieve_ms"] = round((time.perf_counter() - t0) * 1000)
    t1 = time.perf_counter()
    prose = generate_answer(question, context)
    crag_meta["generate_ms"] = round((time.perf_counter() - t1) * 1000)
    # Sort/recency-cutoff only — NOT filter_projects_for_query's entity-overlap
    # drop, which was built for LLM-invented project lists. These cards come
    # from hits the retrieval/rerank/CRAG pipeline already vetted for
    # relevance; a card's clipped ~220-char blurb may just not happen to
    # repeat the query's literal words even though the full chunk matched,
    # and wrongly dropping it hides real articles/records from the answer.
    cards = filter_projects_for_recency(question, build_cards(store, hits))
    crag_meta.update(
        {
            "llm_provider": "anthropic",
            "prompt_variant": _variant_name(),
        }
    )
    return ChatResponse(
        summary=prose,
        projects=cards,
        answer=prose,
        route=RouteKind.RAG.value,
        meta=crag_meta,
    )
