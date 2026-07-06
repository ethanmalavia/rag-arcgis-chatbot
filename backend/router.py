"""Route citizen questions to structured, keyword, or RAG paths."""
from __future__ import annotations

import re

from models import RouteKind

APP_ID_RE = re.compile(r"\b((?:DOS|DCI|LDO|ADD|CPA|REZ)\d{4}-[A-Z]\d{3})\b", re.IGNORECASE)
YEAR_RE = re.compile(r"\b(20\d{2})\b")
AGGREGATE_RE = re.compile(
    r"\b(how many|count|number of|total|list all|how much)\b",
    re.IGNORECASE,
)
NARRATIVE_RE = re.compile(
    r"\b(what happened|why|conditions|summary|decide|decision|explain|tell me about)\b",
    re.IGNORECASE,
)
NAV_RE = re.compile(
    r"\b(minutes|meeting on|document|agenda)\b",
    re.IGNORECASE,
)


def route_question(question: str) -> RouteKind:
    q = question.strip()
    if not q:
        return RouteKind.RAG
    if APP_ID_RE.search(q):
        return RouteKind.KEYWORD
    if AGGREGATE_RE.search(q):
        return RouteKind.STRUCTURED
    if NAV_RE.search(q) and YEAR_RE.search(q):
        return RouteKind.KEYWORD
    if NARRATIVE_RE.search(q):
        return RouteKind.RAG
    # Location / street queries with no narrative cue → keyword + structured blend
    if re.search(r"\b(road|parkway|drive|street|lane|blvd)\b", q, re.IGNORECASE):
        return RouteKind.MIXED
    return RouteKind.RAG
