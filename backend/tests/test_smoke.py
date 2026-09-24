"""Smoke tests for the RAG ArcGIS chatbot backend.

Importing the app module does not build the index (lifespan runs at serve time),
so CI can validate wiring cheaply without model downloads.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import app as backend_app  # noqa: E402
import rag_path  # noqa: E402
from store import DataStore, csv_hash  # noqa: E402


def test_app_metadata():
    assert backend_app.app.title == "Engage Estero RAG API"


def test_expected_routes_registered():
    # Prefer OpenAPI paths — FastAPI may nest included routers without .path.
    paths = set(backend_app.app.openapi().get("paths", {}))
    assert {
        "/health",
        "/ready",
        "/chat",
        "/chat/stream",
        "/load",
        "/feedback",
        "/reports",
        "/admin/status",
        "/recent-decisions",
        "/api/events",
    }.issubset(paths)


def test_csv_hash_is_stable(tmp_path):
    sample = tmp_path / "sample.csv"
    sample.write_text("a,b\n1,2\n", encoding="utf-8")
    first = csv_hash(str(sample))
    second = csv_hash(str(sample))
    assert first == second and len(first) == 32


def _fake_store_with_board_row() -> DataStore:
    import pandas as pd

    df = pd.DataFrame(
        [
            {
                "ApplicationID": "DOS2022-E016",
                "ProjectName": "Wawa Convenience Food & Beverage Store",
                "Location": "10081 Estero Town Commons Place",
                "Outcome": "Approved with staff conditions",
                "MeetingDate": "8/22/2023",
                "Document_Link": "https://example.com/doc.pdf",
                "Latitude": 26.4307,
                "Longitude": -81.7852,
            }
        ]
    )
    return DataStore(dataframe=df)


def test_build_cards_board_record_from_metadata():
    from langchain_core.documents import Document

    store = _fake_store_with_board_row()
    doc = Document(
        page_content="Wawa was approved with conditions.",
        metadata={"application_id": "DOS2022-E016", "row_index": 0, "chunk_type": "action"},
    )
    cards = rag_path.build_cards(store, [(doc, 1.0)])
    assert len(cards) == 1
    assert cards[0].source_type == "board_record"
    assert cards[0].id == "DOS2022-E016"
    assert cards[0].status == "Approved"
    assert cards[0].lat == pytest.approx(26.4307)


def test_build_cards_keeps_board_rows_without_application_id():
    """Most Village Council agenda items (consent agenda, financial reports,
    generic business) have no ApplicationID at all — most PZDB project
    decisions do. Both must still produce a card, keyed by row_index when
    there's no application_id to dedupe on."""
    import pandas as pd
    from langchain_core.documents import Document

    df = pd.DataFrame(
        [
            {
                "ApplicationID": None,
                "ProjectTitle": "APPROVAL OF AGENDA, ADDITIONS, AND DELETIONS",
                "Outcome": "Approved agenda",
                "MeetingDate": "1/3/2024",
                "Document_Link": "https://example.com/minutes.pdf",
            }
        ]
    )
    store = DataStore(dataframe=df)
    doc = Document(
        page_content="Approved agenda.",
        metadata={"application_id": None, "row_index": 0, "chunk_type": "action"},
    )
    cards = rag_path.build_cards(store, [(doc, 1.0)])
    assert len(cards) == 1
    assert cards[0].source_type == "board_record"
    assert cards[0].title == "APPROVAL OF AGENDA, ADDITIONS, AND DELETIONS"


def test_build_cards_article_from_metadata():
    from langchain_core.documents import Document

    store = _fake_store_with_board_row()
    doc = Document(
        page_content=(
            "DATE: 2025-08-14\nSOURCE_TYPE: website_article\nTITLE: Wawa opens\n\n"
            "The new Wawa opened this month with 12 fueling pumps."
        ),
        metadata={
            "source_type": "website_article",
            "record_id": "abc123",
            "title": "Wawa opens",
            "url": "https://esterotoday.com/wawa-opens/",
            "publish_date": "2025-08-14",
            "category": "Development",
        },
    )
    cards = rag_path.build_cards(store, [(doc, 1.0)])
    assert len(cards) == 1
    card = cards[0]
    assert card.source_type == "website_article"
    assert card.article_url == "https://esterotoday.com/wawa-opens/"
    assert card.category == "Development"
    assert "DATE:" not in card.summary
    assert "fueling pumps" in card.summary


def test_build_cards_prefers_latest_row_for_same_application_id():
    """Same application_id can span two dataframe rows (one per meeting it came
    before) — build_cards must keep the row with the latest meeting_date, not
    whichever ranked highest in retrieval."""
    import pandas as pd
    from langchain_core.documents import Document

    df = pd.DataFrame(
        [
            {
                "ApplicationID": "DOS2022-E016",
                "ProjectName": "Wawa Convenience Food & Beverage Store",
                "Location": "10081 Estero Town Commons Place",
                "Outcome": "No decision recorded",
                "MeetingDate": "7/25/2023",
                "Document_Link": "https://example.com/0725.pdf",
            },
            {
                "ApplicationID": "DOS2022-E016",
                "ProjectName": "Wawa Convenience Food & Beverage Store",
                "Location": "10081 Estero Town Commons Place",
                "Outcome": "Approved with staff conditions",
                "MeetingDate": "8/22/2023",
                "Document_Link": "https://example.com/0822.pdf",
            },
        ]
    )
    store = DataStore(dataframe=df)
    earlier = Document(
        page_content="Design review, no action taken.",
        metadata={"application_id": "DOS2022-E016", "row_index": 0, "chunk_type": "action"},
    )
    later = Document(
        page_content="Approved with conditions.",
        metadata={"application_id": "DOS2022-E016", "row_index": 1, "chunk_type": "action"},
    )
    # Retrieval ranked the earlier (non-final) row first, as actually happened.
    cards = rag_path.build_cards(store, [(earlier, 1.0), (later, 0.9)])
    assert len(cards) == 1
    assert cards[0].status == "Approved"
    assert cards[0].date == "8/22/2023"


def test_build_cards_dedupes_and_skips_unciteable_hits():
    from langchain_core.documents import Document

    store = _fake_store_with_board_row()
    doc = Document(
        page_content="Wawa was approved.",
        metadata={"application_id": "DOS2022-E016", "row_index": 0, "chunk_type": "meta"},
    )
    no_url_article = Document(
        page_content="No link for this one.",
        metadata={"source_type": "website_article", "record_id": "x", "title": "No URL"},
    )
    cards = rag_path.build_cards(store, [(doc, 1.0), (doc, 0.9), (no_url_article, 0.5)])
    assert len(cards) == 1  # deduped board record; article without a URL dropped


def test_answer_rag_keeps_both_board_and_article_cards_even_without_keyword_overlap(monkeypatch):
    """Regression: a card whose clipped ~220-char blurb doesn't literally repeat
    the query's words must still survive — it came from a hit the retrieval/
    rerank pipeline already vetted as relevant. Only sorting/recency should be
    applied to deterministic cards, never the entity-overlap drop."""
    from langchain_core.documents import Document

    store = _fake_store_with_board_row()
    board_hit = Document(
        page_content="Wawa was approved.",
        metadata={"application_id": "DOS2022-E016", "row_index": 0, "chunk_type": "action"},
    )
    article_hit = Document(
        page_content="A Summary of Recent and New Developments Planned in Greater Estero.",
        metadata={
            "source_type": "website_article",
            "record_id": "abc",
            "title": "A Summary of Recent and New Developments Planned in Greater Estero",
            "url": "https://esterotoday.com/summary/",
            "publish_date": "2026-01-01",
        },
    )
    monkeypatch.setattr(rag_path, "retrieve_with_crag", lambda s, q: ("ctx", {}, [(board_hit, 1.0), (article_hit, 0.9)]))
    monkeypatch.setattr(rag_path, "generate_answer", lambda q, ctx: "Some prose answer.")

    result = rag_path.answer_rag(store, "What happened with the Wawa development project?")

    source_types = {p.source_type for p in result.projects}
    assert source_types == {"board_record", "website_article"}


class _FakeLLMResult:
    def __init__(self, text: str):
        self.text = text


def _ensure_llm_provider_importable(monkeypatch):
    """llm_provider validates ANTHROPIC_API_KEY at import time — set a fake
    one so this still works in CI with no real key configured. A no-op if
    it's already been imported successfully elsewhere in this process."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-for-unit-tests")
    import llm_provider

    return llm_provider


def test_generate_answer_returns_prose_via_llm_provider(monkeypatch):
    llm_provider = _ensure_llm_provider_importable(monkeypatch)
    prose = "**Wawa** (DOS2022-E016) was approved with staff conditions on August 22, 2023."
    monkeypatch.setattr(llm_provider, "generate", lambda **kwargs: _FakeLLMResult(prose))

    result = rag_path.generate_answer("What happened with the Wawa project?", "some retrieved context")

    assert result == prose


def test_generate_answer_strips_stray_json_fence(monkeypatch):
    llm_provider = _ensure_llm_provider_importable(monkeypatch)
    monkeypatch.setattr(
        llm_provider, "generate", lambda **kwargs: _FakeLLMResult('Some prose.\n```json\n{"a":1}\n```')
    )

    result = rag_path.generate_answer("Any question", "some context")

    assert "```" not in result
    assert result.startswith("Some prose.")


def test_generate_answer_falls_back_when_empty(monkeypatch):
    llm_provider = _ensure_llm_provider_importable(monkeypatch)
    monkeypatch.setattr(llm_provider, "generate", lambda **kwargs: _FakeLLMResult("   "))

    result = rag_path.generate_answer("Any question", "some context")

    assert result == "I don't have records on that."


def test_finalize_prose_trims_trailing_fragment():
    from rag_path import finalize_prose

    assert finalize_prose("This is a complete sentence.") == "This is a complete sentence."
    assert finalize_prose("Short") == "Short"
    assert finalize_prose("A long enough fragment with no ending punct") == (
        "A long enough fragment with no ending punct."
    )


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
    from langchain_core.documents import Document
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
    from langchain_core.documents import Document
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


def test_narrative_phrasing_with_multiple_hits_skips_keyword_shortcut():
    """'What is happening at Wawa' reads as a narrative question, not a tight
    lookup — with several matching rows it must fall through to RAG (a real
    synthesized answer with articles + records) rather than dumping raw rows
    with a generic 'Found N records' one-liner and no prose."""
    from keyword_path import is_strong_keyword_hit
    from models import ChatResponse, ProjectOut

    hit = ChatResponse(
        summary="Found 6 records matching your search.",
        projects=[ProjectOut(title=f"Wawa item {i}", id=f"X{i}") for i in range(6)],
        answer="Found 6 records matching your search.",
        meta={"matched_rows": 6},
    )
    assert not is_strong_keyword_hit(hit, "What is happening at Wawa")
    assert not is_strong_keyword_hit(hit, "What's happening with Wawa?")


def test_prompt_loader_default_and_concise():
    from prompt_loader import clear_prompt_cache, load_prompt

    clear_prompt_cache()
    default_answer = load_prompt("answer", "default")
    assert "Never output a JSON block" in default_answer
    concise_answer = load_prompt("answer", "concise")
    assert "Never output a JSON block" in concise_answer
    assert default_answer != concise_answer


def test_feedback_endpoint_writes_jsonl(tmp_path, monkeypatch):
    import feedback_store
    import models

    feedback_file = tmp_path / "feedback.jsonl"
    monkeypatch.setattr(feedback_store, "FEEDBACK_DIR", str(tmp_path))
    monkeypatch.setattr(feedback_store, "FEEDBACK_FILE", str(feedback_file))

    req = models.FeedbackRequest(
        session_id="test",
        question="What about Wawa?",
        rating="up",
        route="rag",
        summary="- Wawa was discussed.",
        project_ids=["DCI2021-E004"],
    )
    out = feedback_store.append_feedback(req)
    assert out["ok"] is True
    line = feedback_file.read_text(encoding="utf-8").strip()
    payload = __import__("json").loads(line)
    assert payload["rating"] == "up"
    assert payload["question"] == "What about Wawa?"
    assert "DCI2021-E004" in payload["project_ids"]


def test_query_wants_recent_detects_conversational_cues():
    from retrieval import query_wants_recent

    assert query_wants_recent("What are the recent developments?")
    assert query_wants_recent("anything new happening?")
    assert query_wants_recent("latest zoning decisions")
    assert not query_wants_recent("What about Corkscrew Road?")
    assert not query_wants_recent("What was approved in 2017?")


def test_recent_query_hard_filters_old_hits():
    from langchain_core.documents import Document
    from retrieval import apply_recency_boost

    older = Document(
        page_content="meeting_date: 2017-05-15\nSummary: old evidence rules",
        metadata={"chunk_id": "y2017", "meeting_date": "2017-05-15"},
    )
    newer = Document(
        page_content="meeting_date: 2025-03-01\nSummary: new subdivision",
        metadata={"chunk_id": "y2025", "meeting_date": "2025-03-01"},
    )
    # High lexical score on the 2017 hit must not keep it for "recent" queries.
    ranked = apply_recency_boost(
        [(older, 5.0), (newer, 1.0)],
        "What are the recent developments?",
    )
    assert [d.metadata["chunk_id"] for d, _ in ranked] == ["y2025"]


def test_recent_query_empty_when_only_old_hits():
    from langchain_core.documents import Document
    from retrieval import apply_recency_boost, prefer_recent_hits

    older = Document(
        page_content="meeting_date: 2017-05-15\nSummary: old evidence rules",
        metadata={"chunk_id": "y2017", "meeting_date": "2017-05-15"},
    )
    ranked = apply_recency_boost([(older, 5.0)], "What are the recent developments?")
    assert ranked == []
    assert prefer_recent_hits([(older, 5.0)], "recent developments") == []


def test_recency_intent_follows_original_not_rewrite_years():
    from langchain_core.documents import Document
    from retrieval import apply_recency_boost

    older = Document(
        page_content="meeting_date: 2017-05-15\nSummary: old",
        metadata={"chunk_id": "y2017", "meeting_date": "2017-05-15"},
    )
    newer = Document(
        page_content="meeting_date: 2025-03-01\nSummary: new",
        metadata={"chunk_id": "y2025", "meeting_date": "2025-03-01"},
    )
    # Rewrite-shaped retrieval string includes years; intent stays on the original.
    ranked = apply_recency_boost(
        [(older, 5.0), (newer, 1.0)],
        "recent developments Estero 2026 2025",
        intent_query="What are the recent developments?",
    )
    assert [d.metadata["chunk_id"] for d, _ in ranked] == ["y2025"]


def test_rewrite_query_avoids_bare_years(monkeypatch):
    import rag_path

    # Force the deterministic rule-based fallback — a real ANTHROPIC_API_KEY
    # may be configured in this environment, which would otherwise make this
    # test depend on a live, non-deterministic model response.
    monkeypatch.setattr(rag_path, "_haiku_rewrite_query", lambda question: None)

    rewritten = rag_path.rewrite_query("What are the recent developments?")
    assert "recent planning meetings" in rewritten
    assert "2026" not in rewritten
    assert "2025" not in rewritten


def test_filter_projects_for_recency_drops_2017():
    from models import ProjectOut
    from rag_path import filter_projects_for_recency

    old = ProjectOut(title="Old Item", id="O1", date="2017-05-15")
    new = ProjectOut(title="New Item", id="N1", date="2025-06-01")
    kept = filter_projects_for_recency("recent developments", [old, new])
    assert [p.id for p in kept] == ["N1"]


def test_filter_projects_for_recency_empty_when_only_old():
    from models import ProjectOut
    from rag_path import filter_projects_for_recency

    old = ProjectOut(title="Old Item", id="O1", date="2017-05-15")
    older = ProjectOut(title="Older Item", id="O2", date="2015-01-01")
    assert filter_projects_for_recency("recent developments", [old, older]) == []


def test_filter_projects_for_recency_sorts_newest_first():
    from models import ProjectOut
    from rag_path import filter_projects_for_recency

    a = ProjectOut(title="A", id="A", date="2024-01-01")
    b = ProjectOut(title="B", id="B", date="2025-06-01")
    kept = filter_projects_for_recency("latest decisions", [a, b])
    assert [p.id for p in kept] == ["B", "A"]


def test_filter_projects_always_sorts_newest_without_recent_cue():
    from models import ProjectOut
    from rag_path import filter_projects_for_recency

    a = ProjectOut(title="A", id="A", date="2022-01-01")
    b = ProjectOut(title="B", id="B", date="2025-06-01")
    kept = filter_projects_for_recency("What happened on Corkscrew Road?", [a, b])
    assert [p.id for p in kept] == ["B", "A"]


def test_document_date_reads_article_publish_date():
    from langchain_core.documents import Document
    from retrieval import document_meeting_date, format_docs

    older = Document(
        page_content="DATE: 2022-01-15\nSOURCE_TYPE: website_article\nold story",
        metadata={
            "chunk_id": "old-art",
            "source_type": "website_article",
            "publish_date": "2022-01-15",
            "date": "2022-01-15",
        },
    )
    newer = Document(
        page_content="DATE: 2025-11-01\nSOURCE_TYPE: website_article\nnew story",
        metadata={
            "chunk_id": "new-art",
            "source_type": "website_article",
            "publish_date": "2025-11-01",
            "date": "2025-11-01",
        },
    )
    assert document_meeting_date(newer).isoformat() == "2025-11-01"
    ctx = format_docs([(older, 0.9), (newer, 0.5)])
    assert ctx.index("2025-11-01") < ctx.index("2022-01-15")


def test_recency_boost_prefers_newer_article_publish_date():
    from langchain_core.documents import Document
    from retrieval import apply_recency_boost

    older = Document(
        page_content="DATE: 2019-03-01\narticle",
        metadata={"chunk_id": "old", "publish_date": "2019-03-01", "date": "2019-03-01"},
    )
    newer = Document(
        page_content="DATE: 2025-08-01\narticle",
        metadata={"chunk_id": "new", "publish_date": "2025-08-01", "date": "2025-08-01"},
    )
    ranked = apply_recency_boost([(older, 1.0), (newer, 1.0)], "Estero news", boost=0.5)
    assert ranked[0][0].metadata["chunk_id"] == "new"


def test_recent_intent_blocks_keyword_shortcut():
    from keyword_path import is_strong_keyword_hit
    from models import ChatResponse, ProjectOut

    kw = ChatResponse(
        summary="Found 2 records.",
        projects=[
            ProjectOut(title="Old Dev", id="X1", date="2017-05-15"),
            ProjectOut(title="Other", id="X2", date="2018-01-01"),
        ],
        answer="Found 2 records.",
        meta={"matched_rows": 2},
    )
    assert not is_strong_keyword_hit(kw, "What are the recent developments?")
    # App IDs still shortcut even if the question also says "new".
    assert is_strong_keyword_hit(
        ChatResponse(
            summary="Found 1 record.",
            projects=[ProjectOut(title="Wawa", id="DOS2022-E016")],
            answer="Found 1 record.",
            meta={"matched_rows": 1},
        ),
        "DOS2022-E016",
    )


def test_hits_meta_includes_meeting_dates():
    from langchain_core.documents import Document
    from retrieval import hits_meta

    doc = Document(
        page_content="meeting_date: 2025-03-01",
        metadata={"chunk_id": "c1", "meeting_date": "2025-03-01"},
    )
    meta = hits_meta([(doc, 1.0)])
    assert meta["retrieved"] == 1
    assert meta["meeting_dates"] == ["2025-03-01"]
    assert meta["chunk_ids"] == ["c1"]


def test_reserve_by_bucket_keeps_both_source_types():
    """Regression: for a broad topic where one source type (articles) simply
    scores higher across the board, the final result must still reserve a
    couple of slots for the other source type (board records) rather than
    letting the dominant type fill every slot."""
    from langchain_core.documents import Document
    from retrieval import _reserve_by_bucket

    articles = [
        (Document(page_content="a", metadata={"source_type": "website_article"}), 9.0 - i)
        for i in range(6)
    ]
    board = [
        (Document(page_content="b", metadata={"application_id": f"X{i}"}), 3.0 - i)
        for i in range(2)
    ]
    # Articles dominate every score — without reservation they'd fill the cap.
    items = sorted(articles + board, key=lambda t: -t[1])
    result = _reserve_by_bucket(items, min_per_bucket=2, cap=8)
    board_count = sum(1 for d, _ in result if d.metadata.get("application_id"))
    article_count = sum(1 for d, _ in result if d.metadata.get("source_type"))
    assert board_count == 2
    assert article_count == 6


def test_recent_topup_splits_across_source_type_buckets():
    """Regression: articles publish far more often than board meetings happen
    — a naive global newest-N would be filled entirely by articles, leaving
    board records with no recency representation at all."""
    from langchain_core.documents import Document
    from retrieval import _recent_topup
    from store import DataStore

    articles = [
        Document(page_content="a", metadata={"source_type": "website_article", "publish_date": f"2026-09-{10+i:02d}"})
        for i in range(5)
    ]
    board = [
        Document(page_content="b", metadata={"application_id": f"X{i}", "meeting_date": f"2024-0{i+1}-01"})
        for i in range(2)
    ]
    store = DataStore(documents=articles + board)
    topup = _recent_topup(store, 4)
    board_count = sum(1 for d in topup if d.metadata.get("application_id"))
    article_count = sum(1 for d in topup if d.metadata.get("source_type"))
    assert board_count == 2  # both available board docs reserved despite being much older
    assert article_count == 2


def test_reserve_recent_bypasses_threshold_for_newest_per_bucket():
    """Regression: the objectively newest board record and newest article
    must survive into the final pool even if the cross-encoder scored near-
    identical old boilerplate text higher — SCORE_THRESHOLD alone would have
    dropped them (this reproduces the exact bug found testing 'recent agenda
    items approved', which returned nothing newer than 2022 pre-fix)."""
    from langchain_core.documents import Document
    from retrieval import _reserve_recent

    old_board = (Document(page_content="old", metadata={"application_id": "OLD", "meeting_date": "2017-01-01"}), 6.0)
    new_board = (Document(page_content="new", metadata={"application_id": "NEW", "meeting_date": "2026-06-17"}), -9.0)
    new_article = (
        Document(page_content="art", metadata={"source_type": "website_article", "publish_date": "2026-09-11"}),
        -9.5,
    )
    ranked = [old_board, new_board, new_article]
    pool = [old_board]  # only the old, high-scoring item cleared SCORE_THRESHOLD
    result = _reserve_recent(ranked, pool, n=4)
    ids = {d.metadata.get("application_id") or d.metadata.get("source_type") for d, _ in result}
    assert ids == {"OLD", "NEW", "website_article"}


def test_bm25_tokenize_stems_wawas_and_drops_stopwords():
    from store import _tokenize

    toks = _tokenize("are there any new wawas?")
    assert "wawas" in toks
    assert "wawa" in toks
    assert "any" not in toks
    assert "are" not in toks
    assert "there" not in toks


def test_history_intent_disables_recent_only_cutoff():
    """'history and latest information X' wants the whole timeline, so it must
    not trigger the recent-only hard filter that drops every older record."""
    from retrieval import query_wants_history, query_wants_recent

    q = "Give me the history and latest information Coconut point"
    assert query_wants_history(q)
    assert not query_wants_recent(q)
    assert query_wants_recent("latest on Coconut Point")


def test_focus_query_strips_filler_but_keeps_topic():
    from retrieval import focus_query, topic_queries

    assert focus_query("Give me the history and latest information Coconut point") == "Coconut point"
    assert focus_query("latest zoning decisions") == "zoning decisions"
    # Nothing left after stripping -> fall back to the original question.
    assert focus_query("What are the recent developments?") == "What are the recent developments?"
    # 'what is happening' is kept by default and stripped in the second variant.
    assert topic_queries("what is happening at estero parkway") == [
        "what is happening at estero parkway",
        "estero parkway",
    ]


def test_reserve_recent_skips_off_topic_newest_docs_for_specific_query():
    """Regression: 'latest on Coconut Point' got the corpus-wide newest docs
    (I-75, personnel policy) reserved, and prefer_recent_hits then discarded
    every relevant-but-older Coconut Point record in their favour."""
    from langchain_core.documents import Document
    from retrieval import _reserve_recent

    on_topic = (
        Document(page_content="Coconut Point plat", metadata={"application_id": "A", "meeting_date": "2019-01-01"}),
        5.0,
    )
    off_topic = (
        Document(page_content="I-75 expansion impacts", metadata={"source_type": "website_article", "publish_date": "2026-09-15"}),
        -10.0,
    )
    result = _reserve_recent([on_topic, off_topic], [on_topic], n=4, query="Coconut Point")
    assert [d.page_content for d, _ in result] == ["Coconut Point plat"]
    # Generic query with no topic terms keeps the original bypass behaviour.
    result = _reserve_recent([on_topic, off_topic], [on_topic], n=4, query=None)
    assert len(result) == 2


def test_cap_per_record_limits_chunks_from_one_article():
    from langchain_core.documents import Document
    from retrieval import _cap_per_record

    def chunk(i, rec):
        return (Document(page_content=str(i), metadata={"source_type": "website_article", "record_id": rec, "chunk_id": f"{rec}-{i}"}), 1.0 - i * 0.1)

    items = [chunk(0, "a"), chunk(1, "a"), chunk(2, "a"), chunk(3, "a"), chunk(4, "b")]
    kept = _cap_per_record(items, 3)
    assert [d.page_content for d, _ in kept] == ["0", "1", "2", "4"]


def test_development_approval_intent_and_curated_docs():
    """Regression: 'recently approved developments' returned only agenda-approval
    and personnel-policy rows, because the indexed text has no 'development'
    concept while 'approved' matches every procedural row."""
    import pandas as pd
    from langchain_core.documents import Document
    from retrieval import _development_approval_docs, query_wants_development_approvals
    from store import DataStore

    assert query_wants_development_approvals("Tell me about recently approved developments")
    assert query_wants_development_approvals("what projects were approved in 2025")
    assert not query_wants_development_approvals("What is happening on Corkscrew Road")
    # Recency alone is enough for a generic development question…
    assert query_wants_development_approvals("What are the recent developments?")
    # …but any named subject means normal retrieval.
    assert not query_wants_development_approvals("Was the Wawa project approved?")
    assert not query_wants_development_approvals("recent developments on Corkscrew Road")
    assert not query_wants_development_approvals("approved rezoning")
    assert not query_wants_development_approvals("latest news")

    rows = [
        dict(Status="Approved", MeetingDate="2026-01-14", ApplicationType="add", LandUseCategory="commercial_mixed_use_development", FactCategory="vote"),
        dict(Status="Approved", MeetingDate="2026-06-17", ApplicationType="resolution", LandUseCategory="commercial_mixed_use_development", FactCategory="resolution"),
        dict(Status="Approved", MeetingDate="2026-06-17", ApplicationType="", LandUseCategory="meetings_records_public_input", FactCategory="consent_agenda"),
        dict(Status="No Action", MeetingDate="2026-03-10", ApplicationType="dos", LandUseCategory="commercial_mixed_use_development", FactCategory="vote"),
        dict(Status="Approved", MeetingDate="2025-09-09", ApplicationType="dos", LandUseCategory="residential_development", FactCategory="vote"),
    ]
    docs = [
        Document(page_content=f"row {i}", metadata={"row_index": i, "chunk_type": "meta"}) for i in range(len(rows))
    ]
    store = DataStore(dataframe=pd.DataFrame(rows), documents=docs)
    assert [d.metadata["row_index"] for d in _development_approval_docs(store)] == [0, 4]
    assert [d.metadata["row_index"] for d in _development_approval_docs(store, year=2025)] == [4]
    # Not asking about approvals: newest development items of any status (row 3 is "No Action").
    assert [d.metadata["row_index"] for d in _development_approval_docs(store, approved_only=False)] == [3, 0, 4]
