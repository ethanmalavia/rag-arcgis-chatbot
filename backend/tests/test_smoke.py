"""Smoke tests for the RAG ArcGIS chatbot backend.

Importing the app module does not build the index (lifespan runs at serve time),
so CI can validate wiring cheaply without model downloads.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as backend_app  # noqa: E402
from rag_path import parse_structured_answer  # noqa: E402
from store import csv_hash  # noqa: E402


def test_app_metadata():
    assert backend_app.app.title == "Engage Estero RAG API"


def test_expected_routes_registered():
    paths = {route.path for route in backend_app.app.routes}
    assert {"/health", "/ready", "/chat", "/chat/stream", "/load", "/reports", "/admin/status", "/recent-decisions"}.issubset(paths)


def test_csv_hash_is_stable(tmp_path):
    sample = tmp_path / "sample.csv"
    sample.write_text("a,b\n1,2\n", encoding="utf-8")
    first = csv_hash(str(sample))
    second = csv_hash(str(sample))
    assert first == second and len(first) == 32


def test_parse_structured_answer_json():
    raw = (
        '{"summary":"Found one project.",'
        '"projects":[{"title":"Wawa","id":"DOS2022-E016","location":"Estero",'
        '"summary":"Approved with conditions.","status":"Approved",'
        '"date":"8/22/2023","document_url":"https://example.com/doc.pdf"}]}'
    )
    result = parse_structured_answer(raw)
    assert result.summary == "- Found one project."
    assert len(result.projects) == 1
    assert result.projects[0].id == "DOS2022-E016"


def test_parse_structured_answer_strips_markdown_fence():
    raw = '```json\n{"summary":"No match.","projects":[]}\n```'
    result = parse_structured_answer(raw)
    assert result.summary == "- No match."
    assert result.projects == []
    assert result.meta.get("parse_ok") is True


def test_choose_llm_tier_collaborate_when_both_keys(monkeypatch):
    from rag_path import choose_llm_tier

    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("GROQ_API_KEY", "q")
    assert choose_llm_tier("Explain RiverCreek") == "collaborate"
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    assert choose_llm_tier("Corkscrew Road") == "gemini"


def test_format_summary_bullets():
    from rag_path import format_summary_bullets

    out = format_summary_bullets("Project A was approved. Project B was continued.")
    assert out.startswith("- ")
    assert "\n- " in out
    assert format_summary_bullets("- One thing.\n- Two thing.") == "- One thing.\n- Two thing."


def test_stale_source_notice_when_older_than_five_years():
    from datetime import date

    from models import ChatResponse, ProjectOut
    from stale_sources import attach_stale_source_notice, stale_notice_meta

    meta = stale_notice_meta(
        [date(2018, 5, 1), date(2024, 1, 1)],
        today=date(2026, 7, 15),
        threshold_years=5,
    )
    assert meta["stale_sources"] is True
    assert "2018-05-01" in meta["stale_notice"]
    assert "2018-05-01" in meta["stale_source_dates"]

    fresh = stale_notice_meta([date(2024, 1, 1)], today=date(2026, 7, 15), threshold_years=5)
    assert fresh["stale_sources"] is False

    result = ChatResponse(
        summary="- something",
        projects=[ProjectOut(title="Old", date="01/15/2019")],
        answer="- something",
    )
    attach_stale_source_notice(result)
    assert result.meta.get("stale_sources") is True
    assert "stale_notice" in result.meta


def test_recency_boost_prefers_newer_when_no_year():
    from langchain.schema import Document
    from retrieval import apply_recency_boost

    older = Document(
        page_content="meeting_date: 2018-01-01\nSummary: old road work",
        metadata={"chunk_id": "old", "meeting_date": "2018-01-01"},
    )
    newer = Document(
        page_content="meeting_date: 2025-06-01\nSummary: new road work",
        metadata={"chunk_id": "new", "meeting_date": "2025-06-01"},
    )
    # Same relevance score — recency should put 2025 first.
    ranked = apply_recency_boost([(older, 1.0), (newer, 1.0)], "Corkscrew Road", boost=0.5)
    assert ranked[0][0].metadata["chunk_id"] == "new"


def test_recency_boost_honors_year_in_query():
    from langchain.schema import Document
    from retrieval import apply_recency_boost

    d2018 = Document(
        page_content="meeting_date: 2018-05-01\nSummary: approved in 2018",
        metadata={"chunk_id": "y2018", "meeting_date": "2018-05-01"},
    )
    d2025 = Document(
        page_content="meeting_date: 2025-05-01\nSummary: approved in 2025",
        metadata={"chunk_id": "y2025", "meeting_date": "2025-05-01"},
    )
    ranked = apply_recency_boost([(d2025, 1.0), (d2018, 1.0)], "What was approved in 2018?", boost=0.5)
    assert ranked[0][0].metadata["chunk_id"] == "y2018"


def test_keyword_shortcut_for_app_id():
    from keyword_path import is_strong_keyword_hit
    from models import ChatResponse, ProjectOut

    hit = ChatResponse(
        summary="Found 1 record.",
        projects=[ProjectOut(title="Wawa", id="DOS2022-E016")],
        answer="Found 1 record.",
        meta={"matched_rows": 1},
    )
    assert is_strong_keyword_hit(hit, "DOS2022-E016")
    miss = ChatResponse(summary="none", projects=[], answer="none", meta={"matched_rows": 0})
    assert not is_strong_keyword_hit(miss, "Corkscrew Road")
