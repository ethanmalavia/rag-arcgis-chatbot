"""Orchestrate router-first answers across structured, keyword, and RAG paths."""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from typing import Any

from fastapi import HTTPException

from events_path import answer_upcoming_events, is_events_question
from models import ChatResponse, RouteKind
from rag_path import (
    StructuredAnswer,
    answer_rag,
    build_rag_response,
    generate_answer_stream,
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
            # Aggregate counts ("how many X") stay a deterministic pandas
            # count, not an LLM call — an LLM synthesizing a count from a
            # handful of retrieved chunks would risk fabricating a number
            # (the one thing the answer prompt explicitly forbids). Every
            # other route — including what used to be a keyword-shortcut
            # bypass for short/tight queries — now always goes through
            # answer_rag's full query-rewrite -> retrieve -> rerank -> LLM
            # pipeline below, per "every question must go through the LLM".
            result = answer_structured(store.dataframe, question)
        else:
            result = answer_rag(store, question)
            if route == RouteKind.MIXED:
                result.route = RouteKind.MIXED.value
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
    """SSE: meta → generate (single Claude call, answer text streamed as written) → done."""
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
        # Deterministic aggregate count — see the matching comment in
        # answer_question for why this one route stays off the LLM.
        result = answer_structured(store.dataframe, question)
        result.meta["latency_ms"] = round((time.perf_counter() - t0) * 1000)
        attach_stale_source_notice(result)
        yield _sse({"type": "done", **result.model_dump()})
        return

    # Every other route — no more keyword-shortcut bypass — goes through the
    # full query-rewrite -> retrieve -> rerank -> LLM pipeline below.
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

    # Single Claude call writing a structured JSON answer grounded in the
    # retrieved context. Its answer_markdown field is streamed as tokens
    # while the rest of the JSON (timeline, related, follow-ups) is still
    # being written; "done" then carries the validated final answer. Cards
    # are never LLM-authored — built deterministically from the same hits'
    # metadata, filtered to used_record_ids (build_rag_response, shared with
    # the non-streaming answer_rag).
    structured: StructuredAnswer | None = None
    for item in generate_answer_stream(question, context, crag_meta.get("retrieved_record_ids")):
        if isinstance(item, StructuredAnswer):
            structured = item
            continue
        if first_token_ms is None:
            first_token_ms = round((time.perf_counter() - t0) * 1000)
        yield _sse({"type": "token", "text": item})
    assert structured is not None  # generate_answer_stream always ends with one
    result = build_rag_response(store, question, hits, structured, crag_meta)
    if route == RouteKind.MIXED:
        result.route = RouteKind.MIXED.value
    if first_token_ms is None and result.answer:
        # Nothing streamed (fallback / error path) — send the final text once.
        first_token_ms = round((time.perf_counter() - t0) * 1000)
        yield _sse({"type": "token", "text": result.answer})
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
