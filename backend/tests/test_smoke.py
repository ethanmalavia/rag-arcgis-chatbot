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
    assert {"/health", "/ready", "/chat", "/chat/stream", "/load"}.issubset(paths)


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
    assert result.summary == "Found one project."
    assert len(result.projects) == 1
    assert result.projects[0].id == "DOS2022-E016"


def test_parse_structured_answer_strips_markdown_fence():
    raw = '```json\n{"summary":"No match.","projects":[]}\n```'
    result = parse_structured_answer(raw)
    assert result.summary == "No match."
    assert result.projects == []
