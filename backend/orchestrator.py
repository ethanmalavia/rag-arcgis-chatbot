"""Orchestrate router-first answers across structured, keyword, and RAG paths."""
from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from fastapi import HTTPException

from keyword_path import answer_keyword
from models import ChatResponse, RouteKind
from rag_path import answer_rag, parse_structured_answer, retrieve_with_crag, stream_llm_tokens
from router import route_question
from store import get_store
from structured_path import answer_structured
from tracing import trace_span


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

    route = route_question(question)
    with trace_span("answer_question", {"route": route.value, "question": question[:120]}):
        if route == RouteKind.STRUCTURED:
            return answer_structured(store.dataframe, question)
        if route == RouteKind.KEYWORD:
            return answer_keyword(store.dataframe, question)
        if route == RouteKind.MIXED:
            kw = answer_keyword(store.dataframe, question)
            if kw.projects:
                kw.route = RouteKind.MIXED.value
                kw.meta["paths"] = ["keyword"]
                return kw
            return answer_rag(store, question)
        return answer_rag(store, question)


def stream_answer(question: str) -> Iterator[str]:
    """SSE events: meta → tokens (RAG only) → done."""
    store = get_store()
    if store is None or not store.is_ready():
        yield _sse({"type": "error", "detail": "No dataset loaded"})
        return

    route = route_question(question)
    yield _sse({"type": "meta", "route": route.value})

    if route == RouteKind.STRUCTURED:
        result = answer_structured(store.dataframe, question)
        yield _sse({"type": "done", **result.model_dump()})
        return
    if route == RouteKind.KEYWORD:
        result = answer_keyword(store.dataframe, question)
        yield _sse({"type": "done", **result.model_dump()})
        return
    if route == RouteKind.MIXED:
        kw = answer_keyword(store.dataframe, question)
        if kw.projects:
            kw.route = RouteKind.MIXED.value
            yield _sse({"type": "done", **kw.model_dump()})
            return

    context, crag_meta = retrieve_with_crag(store, question)
    yield _sse({"type": "meta", "route": RouteKind.RAG.value, **crag_meta})

    buffer = ""
    for token in stream_llm_tokens(question, context):
        buffer += token
        yield _sse({"type": "token", "text": token})

    result = parse_structured_answer(buffer, route=RouteKind.RAG.value)
    result.meta.update(crag_meta)
    yield _sse({"type": "done", **result.model_dump()})


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload)}\n\n"
