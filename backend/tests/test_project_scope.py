"""Project-scoped retrieval: dominant-project detection, recall expansion,
precision filtering, and the enable flag."""
from __future__ import annotations

import sys
import types
from pathlib import Path

from langchain.schema import Document

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import retrieval
from retrieval import dominant_project_id, scope_hits_to_project


def _doc(row_index: int, project_id: str, date: str = "2024-01-01") -> Document:
    return Document(
        page_content=f"row {row_index}",
        metadata={
            "chunk_id": f"{row_index}-meta",
            "chunk_type": "meta",
            "row_index": row_index,
            "project_id": project_id,
            "meeting_date": date,
        },
    )


def _fake_store(docs: list[Document]):
    return types.SimpleNamespace(documents=docs)


def test_dominant_project_needs_min_support():
    # one distinct linked item -> not enough support
    hits = [(_doc(1, "3"), 0.9), (_doc(2, ""), 0.8)]
    assert dominant_project_id(hits, 2) is None
    # two distinct linked items of the same project -> dominant
    hits = [(_doc(1, "3"), 0.9), (_doc(2, "3"), 0.8)]
    assert dominant_project_id(hits, 2) == "3"


def test_dominant_ignores_repeated_chunks_of_one_row():
    # same row_index appearing twice must not fake support
    hits = [(_doc(1, "3"), 0.9), (_doc(1, "3"), 0.7)]
    assert dominant_project_id(hits, 2) is None


def test_scope_expands_recall_and_drops_conflicting_project():
    corpus = [_doc(i, "3", f"2024-0{i}-01") for i in range(1, 6)]
    corpus.append(_doc(9, "7"))  # a different project
    store = _fake_store(corpus)
    # hits: two linked items + one from a different project
    hits = [(corpus[0], 1.0), (corpus[1], 0.9), (corpus[5], 0.8)]
    scoped = scope_hits_to_project(store, hits)
    pids = {d.metadata["project_id"] for d, _ in scoped}
    assert pids == {"3"}  # conflicting project 7 dropped
    # all five linked items pulled in (recall)
    assert len({d.metadata["row_index"] for d, _ in scoped}) == 5


def test_unlinked_hit_demoted_below_linked_set():
    corpus = [_doc(i, "3", f"2024-0{i}-01") for i in range(1, 4)]
    unlinked = _doc(99, "", "2024-12-01")
    store = _fake_store(corpus)
    hits = [(corpus[0], 1.0), (corpus[1], 0.9), (unlinked, 0.95)]
    scoped = scope_hits_to_project(store, hits)
    order = [d.metadata["row_index"] for d, _ in scoped]
    # unlinked item is last, after every project-3 record
    assert order[-1] == 99
    assert all(
        order.index(d.metadata["row_index"]) < order.index(99)
        for d, _ in scoped
        if d.metadata["project_id"] == "3"
    )


def test_expansion_capped_and_recency_ordered():
    old_cap = retrieval.PROJECT_SCOPE_CAP
    retrieval.PROJECT_SCOPE_CAP = 3
    try:
        corpus = [_doc(i, "3", f"2020-01-{i:02d}") for i in range(1, 11)]
        store = _fake_store(corpus)
        hits = [(corpus[0], 1.0), (corpus[1], 0.9)]  # two linked -> dominant
        scoped = scope_hits_to_project(store, hits)
        assert len(scoped) == 3  # capped
        # the two original hits are retained (highest scores lead)
        assert {corpus[0].metadata["row_index"], corpus[1].metadata["row_index"]} <= {
            d.metadata["row_index"] for d, _ in scoped
        }
    finally:
        retrieval.PROJECT_SCOPE_CAP = old_cap


def test_disabled_flag_is_passthrough():
    old = retrieval.ENABLE_PROJECT_SCOPE
    retrieval.ENABLE_PROJECT_SCOPE = False
    try:
        corpus = [_doc(i, "3") for i in range(1, 6)]
        store = _fake_store(corpus)
        hits = [(corpus[0], 1.0), (corpus[1], 0.9)]
        assert scope_hits_to_project(store, hits) == hits
    finally:
        retrieval.ENABLE_PROJECT_SCOPE = old
