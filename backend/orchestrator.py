"""Orchestrate router-first answers across structured, keyword, and RAG paths."""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from typing import Any

from fastapi import HTTPException

from config import ENABLE_LLM_COLLABORATE, GEMINI_MODEL, GROQ_MODEL
from keyword_path import answer_keyword, is_strong_keyword_hit
from models import ChatResponse, RouteKind
from rag_path import (
    answer_rag,
    choose_llm_tier,
    finalize_prose,
    gemini_available,
    gemini_extract_projects,
    generate_answer,
    groq_available,
    groq_write_summary,
    retrieve_with_crag,
    stream_groq_summary,
)
from router import route_question
from store import get_store
from structured_path import answer_structured
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

    route = route_question(question)
    t0 = time.perf_counter()
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
        logger.info(
            "answer_question route=%s mode=%s total_ms=%s",
            result.route,
            result.meta.get("llm_mode") or result.meta.get("paths"),
            total_ms,
        )
        return result


def stream_answer(question: str) -> Iterator[str]:
    """SSE: meta → (collaborate: extract then summary tokens) → done."""
    store = get_store()
    if store is None or not store.is_ready():
        yield _sse({"type": "error", "detail": "No dataset loaded"})
        return

    route = route_question(question)
    t0 = time.perf_counter()
    yield _sse({"type": "meta", "route": route.value})

    if route == RouteKind.STRUCTURED:
        result = answer_structured(store.dataframe, question)
        result.meta["latency_ms"] = round((time.perf_counter() - t0) * 1000)
        yield _sse({"type": "done", **result.model_dump()})
        return

    shortcut = _try_keyword_shortcut(store, question)
    if shortcut is not None:
        if route == RouteKind.MIXED:
            shortcut.route = RouteKind.MIXED.value
        shortcut.meta["latency_ms"] = round((time.perf_counter() - t0) * 1000)
        yield _sse({"type": "done", **shortcut.model_dump()})
        return

    t_retrieve = time.perf_counter()
    context, crag_meta = retrieve_with_crag(store, question)
    retrieve_ms = round((time.perf_counter() - t_retrieve) * 1000)
    crag_meta["retrieve_ms"] = retrieve_ms

    use_collab = (
        gemini_available()
        and groq_available()
        and ENABLE_LLM_COLLABORATE
    )
    mode = choose_llm_tier(question, crag_meta)
    yield _sse({
        "type": "meta",
        "route": RouteKind.RAG.value,
        "llm_mode": mode,
        **crag_meta,
    })

    t_gen = time.perf_counter()
    first_token_ms: int | None = None

    if use_collab:
        try:
            yield _sse({"type": "meta", "phase": "extract", "provider": "gemini", "model": GEMINI_MODEL})
            t_ex = time.perf_counter()
            projects = gemini_extract_projects(question, context)
            extract_ms = round((time.perf_counter() - t_ex) * 1000)

            yield _sse({"type": "meta", "phase": "summary", "provider": "groq", "model": GROQ_MODEL})
            buffer = ""
            t_sum = time.perf_counter()
            for token in stream_groq_summary(question, projects):
                if first_token_ms is None:
                    first_token_ms = round((time.perf_counter() - t0) * 1000)
                buffer += token
                yield _sse({"type": "token", "text": token})
            summary = finalize_prose(buffer) or groq_write_summary(question, projects)
            projects = projects[:5]
            for p in projects:
                p.summary = finalize_prose(p.summary)
            summary_ms = round((time.perf_counter() - t_sum) * 1000)

            result = ChatResponse(
                summary=summary,
                projects=projects,
                answer=summary,
                route=RouteKind.RAG.value,
                meta={
                    **crag_meta,
                    "parse_ok": True,
                    "llm_mode": "collaborate",
                    "llm_providers": ["gemini", "groq"],
                    "llm_models": {"extract": GEMINI_MODEL, "summary": GROQ_MODEL},
                    "extract_ms": extract_ms,
                    "summary_ms": summary_ms,
                    "generate_ms": round((time.perf_counter() - t_gen) * 1000),
                    "ttft_ms": first_token_ms,
                    "latency_ms": round((time.perf_counter() - t0) * 1000),
                },
            )
            yield _sse({"type": "done", **result.model_dump()})
            return
        except Exception as e:
            logger.warning("Collaborate stream failed (%s); solo fallback", e)

    # Solo fallback: generate full answer (optional token stream from one provider).
    result = generate_answer(question, context, crag_meta=crag_meta)
    # If we already have a complete result, stream the summary as tokens for UX.
    summary = result.summary or result.answer or ""
    if summary and first_token_ms is None:
        first_token_ms = round((time.perf_counter() - t0) * 1000)
        yield _sse({"type": "token", "text": summary})
    result.meta.update(crag_meta)
    result.meta["generate_ms"] = round((time.perf_counter() - t_gen) * 1000)
    result.meta["ttft_ms"] = first_token_ms
    result.meta["latency_ms"] = round((time.perf_counter() - t0) * 1000)
    yield _sse({"type": "done", **result.model_dump()})


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, default=_json_default)}\n\n"


def _json_default(obj: Any) -> Any:
    if hasattr(obj, "item"):
        return obj.item()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")
