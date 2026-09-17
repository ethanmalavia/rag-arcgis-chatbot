"""Golden Q&A and router tests (no LLM API key required)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import DEFAULT_CSV_PATH
from schema_aliases import load_dataframe
from keyword_path import answer_keyword
from router import route_question
from structured_path import answer_structured

GOLDEN_PATH = Path(__file__).parent / "golden_qa.json"


@pytest.fixture(scope="module")
def dataframe():
    if not Path(DEFAULT_CSV_PATH).exists():
        pytest.skip("meetings corpus CSV not present")
    return load_dataframe(DEFAULT_CSV_PATH)


@pytest.fixture(scope="module")
def golden_cases():
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


def test_router_matches_golden_expectations(golden_cases):
    for case in golden_cases:
        got = route_question(case["question"]).value
        assert got == case["route"], f"{case['question']!r} -> {got}, want {case['route']}"


def test_structured_count_has_rows(dataframe, golden_cases):
    case = next(c for c in golden_cases if c.get("min_rows") is not None and c["min_rows"] >= 1)
    result = answer_structured(dataframe, case["question"])
    assert result.meta.get("matched_rows", 0) >= case["min_rows"]


def test_keyword_finds_application_id(dataframe, golden_cases):
    case = next(
        c
        for c in golden_cases
        if c.get("expect_ids") and c.get("route") == "keyword" and len(c["question"].split()) == 1
    )
    result = answer_keyword(dataframe, case["question"])
    ids = {p.id.upper() for p in result.projects}
    for expected in case["expect_ids"]:
        assert any(expected.upper() in i for i in ids), f"missing {expected} in {ids}"
