"""Orchestrate router-first answers across structured, keyword, and RAG paths."""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from typing import Any

from fastapi import HTTPException

from config import PROMPT_VARIANT
from events_path import answer_upcoming_events, is_events_question
from keyword_path import answer_keyword, is_strong_keyword_hit
from models import ChatResponse, RouteKind
from rag_path import (
    answer_rag,
    build_cards,
    filter_projects_for_recency,
    generate_answer,
    retrieve_with_crag,
)
from router import route_question
from stale_sources import attach_stale_source_notice
from store import get_store
from structured_path import answer_structured, attach_coords
from tracing import trace_span

logger = logging.getLogger(__name__)


def _dedupe_projects(projects: list) -> list:
    seen: set[str] = set()
    out = []
    for p in projects:
        key = p.id or p.title
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def _try_keyword_shortcut(store, question: str) -> ChatResponse | None:
    """Skip LLM when keyword/lookup match is tight enough."""
    kw = answer_keyword(store.dataframe, question)
    if is_strong_keyword_hit(kw, question):
        kw.meta["llm_skipped"] = True
        kw.meta["paths"] = ["keyword"]
        return kw
    return None


def answer_question(question: str) -> ChatResponse:
    store = get_store()
    if store is None or not store.is_ready():
        raise HTTPException(503, "No dataset loaded. Use Load CSV in the UI first.")

    t0 = time.perf_counter()
    if is_events_question(question, store.dataframe):
        result = answer_upcoming_events(question)
        result.meta["latency_ms"] = round((time.perf_counter() - t0) * 1000)
        attach_coords(result.projects, store.dataframe)
        logger.info(
            "answer_question route=%s mode=%s total_ms=%s",
            result.route,
            result.meta.get("paths"),
            result.meta["latency_ms"],
        )
        return result

    route = route_question(question)
    with trace_span("answer_question", {"route": route.value, "question": question[:120]}):
        if route == RouteKind.STRUCTURED:
            result = answer_structured(store.dataframe, question)
        else:
            # Only skip the LLM for tight hits (app IDs / few rows). Broad
            # street/topic matches go through RAG so the summary makes sense.
            shortcut = _try_keyword_shortcut(store, question)
            if shortcut is not None:
                if route == RouteKind.MIXED:
                    shortcut.route = RouteKind.MIXED.value
                result = shortcut
            else:
                result = answer_rag(store, question)
        total_ms = round((time.perf_counter() - t0) * 1000)
        result.meta["latency_ms"] = total_ms
        attach_coords(result.projects, store.dataframe)
        attach_stale_source_notice(result)
        logger.info(
            "answer_question route=%s mode=%s total_ms=%s stale=%s",
            result.route,
            result.meta.get("llm_mode") or result.meta.get("paths"),
            total_ms,
            result.meta.get("stale_sources"),
        )
        return result


def stream_answer(question: str) -> Iterator[str]:
    """SSE: meta → generate (single Claude call, streamed as one token) → done."""
    store = get_store()
    if store is None or not store.is_ready():
        yield _sse({"type": "error", "detail": "No dataset loaded"})
        return

    t0 = time.perf_counter()
    if is_events_question(question, store.dataframe):
        result = answer_upcoming_events(question)
        result.meta["latency_ms"] = round((time.perf_counter() - t0) * 1000)
        yield _sse({"type": "meta", "route": RouteKind.EVENTS.value})
        if result.summary:
            yield _sse({"type": "token", "text": result.summary})
        yield _sse({"type": "done", **result.model_dump()})
        return

    route = route_question(question)
    yield _sse({"type": "meta", "route": route.value})

    if route == RouteKind.STRUCTURED:
        result = answer_structured(store.dataframe, question)
        result.meta["latency_ms"] = round((time.perf_counter() - t0) * 1000)
        attach_stale_source_notice(result)
        yield _sse({"type": "done", **result.model_dump()})
        return

    shortcut = _try_keyword_shortcut(store, question)
    if shortcut is not None:
        if route == RouteKind.MIXED:
            shortcut.route = RouteKind.MIXED.value
        shortcut.meta["latency_ms"] = round((time.perf_counter() - t0) * 1000)
        attach_stale_source_notice(shortcut)
        yield _sse({"type": "done", **shortcut.model_dump()})
        return

    t_retrieve = time.perf_counter()
    context, crag_meta, hits = retrieve_with_crag(store, question)
    retrieve_ms = round((time.perf_counter() - t_retrieve) * 1000)
    crag_meta["retrieve_ms"] = retrieve_ms

    yield _sse({
        "type": "meta",
        "route": RouteKind.RAG.value,
        "llm_mode": "claude",
        **crag_meta,
    })

    t_gen = time.perf_counter()
    first_token_ms: int | None = None

    # Single Claude call: writes free-form prose grounded in the retrieved
    # context, then streams it as one token. Cards are never LLM-authored —
    # built deterministically from the same hits' metadata (build_cards).
    prose = generate_answer(question, context)
    # Sort/recency-cutoff only — see rag_path.answer_rag for why the
    # entity-overlap filter is skipped for deterministic cards.
    cards = filter_projects_for_recency(question, build_cards(store, hits))
    if prose:
        first_token_ms = round((time.perf_counter() - t0) * 1000)
        yield _sse({"type": "token", "text": prose})
    crag_meta.update({"llm_provider": "anthropic", "prompt_variant": PROMPT_VARIANT})
    result = ChatResponse(
        summary=prose,
        projects=cards,
        answer=prose,
        route=RouteKind.RAG.value,
        meta=crag_meta,
    )
    result.meta["generate_ms"] = round((time.perf_counter() - t_gen) * 1000)
    result.meta["ttft_ms"] = first_token_ms
    result.meta["latency_ms"] = round((time.perf_counter() - t0) * 1000)
    attach_coords(result.projects, store.dataframe)
    attach_stale_source_notice(result)
    yield _sse({"type": "done", **result.model_dump()})


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, default=_json_default)}\n\n"


def _json_default(obj: Any) -> Any:
    if hasattr(obj, "item"):
        return obj.item()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")
