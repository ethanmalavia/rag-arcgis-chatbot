"""Unit tests for community events normalize / chat intent (no live network)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from events_path import is_events_question  # noqa: E402
from events_sources.normalize import (  # noqa: E402
    dedupe_events,
    is_local_enough,
    make_event,
    map_category,
    sanitize_category,
)


def test_map_category_village_and_keywords():
    assert map_category(title="Council Meeting", source_slugs=["village-of-estero-event"]) == "government"
    # Government slug wins over title keywords that would otherwise say "music"
    assert (
        map_category(
            title="Council workshop on live music venues",
            source_slugs=["village-of-estero-event"],
        )
        == "government"
    )
    assert map_category(title="Jazz Concert at the Park", source_slugs=["community"]) == "music"
    assert map_category(title="Weekend Flea Market", source_slugs=["other-event"]) == "market"
    assert map_category(title="Lee County Fairgrounds Carnival", source_slugs=[]) == "fair"
    assert map_category(title="FGCU Soccer vs USF", source_slugs=["sports"]) == "sports"
    assert map_category(title="Mystery meetup", source_slugs=["totally-unknown-slug"]) == "community"


def test_geo_filter_keeps_local_drops_far():
    assert is_local_enough("Home game", "Fort Myers, Fla., FGCU Soccer Complex")
    assert is_local_enough("Market", "Coconut Point, Estero")
    assert not is_local_enough("Away game", "Orlando, Fla.")
    assert not is_local_enough(
        "FGCU Women's Soccer at LSU",
        "Baton Rouge, La.",
    )
    assert not is_local_enough(
        "FGCU Volleyball vs Someone",
        "Atlanta, Ga.",
    )
    assert is_local_enough(
        "FGCU Women's Soccer vs FIU",
        "Fort Myers, Fla., FGCU Soccer Complex",
    )


def test_sanitize_category_clamps_unknown():
    assert sanitize_category("sports") == "sports"
    assert sanitize_category("village") == "government"
    assert sanitize_category("engage-estero") == "community"
    assert sanitize_category("nope") == "other"
    assert sanitize_category(None) == "other"
    ev = make_event(
        id="x",
        title="T",
        start="2026-09-01T10:00:00",
        end="2026-09-01T11:00:00",
        all_day=False,
        venue=None,
        url="",
        category="weird",
        source="manual",
    )
    assert ev["category"] == "other"


def test_lee_county_html_parser_extracts_lakes_park():
    from events_sources.lee_county import _parse_calendar_html

    html = """
    <span class="calendar_eventtime">8:00 AM</span>
    <a class="eventLink" title="Lakes Park Bird Patrol Walk"
       onclick="gotoEvent('/parks/events/event','%7B64d50b61-9b75-485a-b40d-10ff8595e178%7D%3B876.0.2099-09-05T12%3A00%3A00Z',true)"
       class="calendar_eventlink">Lakes Park Bird Patrol Walk</a>
    """
    events = _parse_calendar_html(html)
    assert len(events) == 1
    assert events[0]["title"] == "Lakes Park Bird Patrol Walk"
    assert events[0]["source"] == "lee_county"
    assert events[0]["start"].startswith("2099-09-05")
    assert "Lakes Park" in (events[0].get("venue") or "")


def test_visitfortmyers_detail_and_rss_parsers():
    from events_sources.visitfortmyers import _parse_detail, _parse_rss_items

    rss = """<?xml version="1.0"?>
    <rss version="2.0"><channel>
      <item><title>Makers Monday</title>
        <link>https://www.visitfortmyers.com/event/makers-monday/8270</link></item>
    </channel></rss>"""
    items = _parse_rss_items(rss)
    assert items[0]["title"] == "Makers Monday"

    html = """
    <p class="address"><span class="address-line1">13211 McGregor Blvd</span>
    <span class="locality">Fort Myers</span></p>
    <var class="atc_date_start">2099-06-25 19:30:00</var>
    <var class="atc_date_end">2099-06-25 21:00:00</var>
    """
    parsed = _parse_detail(html)
    assert parsed["start"] == "2099-06-25T19:30:00"
    assert "Fort Myers" in (parsed["venue"] or "")


def test_dedupe_by_title_date_venue():
    a = make_event(
        id="1",
        title="Farmers Market",
        start="2026-09-06T08:00:00",
        end="2026-09-06T13:00:00",
        all_day=False,
        venue="Estero",
        url="",
        category="market",
        source="manual",
    )
    b = make_event(
        id="2",
        title="Farmers Market",
        start="2026-09-06T08:00:00",
        end="2026-09-06T13:00:00",
        all_day=False,
        venue="Estero",
        url="https://example.com",
        category="market",
        source="esterotoday",
    )
    c = make_event(
        id="3",
        title="Farmers Market",
        start="2026-09-13T08:00:00",
        end="2026-09-13T13:00:00",
        all_day=False,
        venue="Estero",
        url="",
        category="market",
        source="manual",
    )
    out = dedupe_events([a, b, c])
    assert len(out) == 2
    assert out[0]["id"] == "1"


@pytest.mark.parametrize(
    "question,expected",
    [
        ("What's happening this weekend?", True),
        ("Any concerts near Estero?", True),
        ("upcoming flea markets", True),
        ("FGCU sports events this week", True),
        ("things to do this weekend", True),
        ("live music near Estero", True),
        # Planning/zoning language must NOT take the events shortcut
        ("What was approved on Corkscrew?", False),
        ("what is happening on Corkscrew Road", False),
        ("What's happening with the Wawa rezoning?", False),
        ("upcoming events for the zoning board hearing", False),
        ("DOS2024-E001", False),
        ("How many records were approved in 2023?", False),
        # Topic questions that merely open with "what is happening" are RAG, not the calendar.
        ("What is happening to I-75", False),
        ("What is happening with the interstate expansion", False),
        ("What's happening in Estero?", True),
        ("what's happening at the park this weekend", True),
    ],
)
def test_is_events_question(question, expected):
    assert is_events_question(question) is expected


def test_is_events_question_vetoed_by_dataset_keyword_hit():
    """PLANNING_SIGNAL_RE only catches street-suffix/zoning jargon — a bare
    named project ("What is happening at Wawa") has neither, so without a
    dataset check it would wrongly take the generic events shortcut."""
    import pandas as pd

    df = pd.DataFrame(
        [{"ProjectName": "Wawa Convenience Food & Beverage Store", "ApplicationID": "DOS2022-E016"}]
    )
    assert is_events_question("What is happening at Wawa", df) is False
    # A real events question with no dataset hit still takes the shortcut.
    assert is_events_question("What's happening this weekend?", df) is True


def test_answer_upcoming_events_formats_bullets(monkeypatch):
    from events_path import answer_upcoming_events

    today = __import__("datetime").date.today()
    start = f"{today.isoformat()}T18:00:00"
    monkeypatch.setattr(
        "events_path.list_upcoming_events",
        lambda: [
            {
                "id": "et-1",
                "title": "Estero Farmers Market",
                "start": start,
                "end": start,
                "allDay": False,
                "venue": "Coconut Point",
                "url": "https://esterotoday.com/events/example",
                "category": "market",
                "source": "esterotoday",
            }
        ],
    )
    # Use a 21-day window query (not weekend-only) so the test is weekday-safe.
    result = answer_upcoming_events("upcoming flea markets")
    assert result.route == "events"
    assert result.meta.get("llm_skipped") is True
    assert "Farmers Market" in result.summary
    assert result.summary.strip().startswith("- ")


def test_answer_upcoming_events_empty_window(monkeypatch):
    from events_path import answer_upcoming_events

    monkeypatch.setattr("events_path.list_upcoming_events", lambda: [])
    result = answer_upcoming_events("any concerts this weekend?")
    assert result.route == "events"
    assert result.meta.get("events_count") == 0
    assert "don't have upcoming" in result.summary.lower()


def test_ics_util_parses_utc_and_all_day():
    from events_sources.ics_util import clean_ics_text, iter_vevents, parse_ics_dt

    iso, all_day = parse_ics_dt("20260915")
    assert iso.startswith("2026-09-15T00:00:00")
    assert all_day is True

    iso_z, all_day_z = parse_ics_dt("20260915T163000Z")
    assert all_day_z is False
    assert iso_z.startswith("2026-09-15T")

    body = (
        "BEGIN:VCALENDAR\n"
        "BEGIN:VEVENT\n"
        "SUMMARY:FGCU Soccer\\, Home\n"
        "DTSTART:20260920T190000Z\n"
        "LOCATION:Fort Myers\\, Fla.\n"
        "END:VEVENT\n"
        "END:VCALENDAR\n"
    )
    blocks = list(iter_vevents(body))
    assert len(blocks) == 1
    assert "FGCU Soccer" in clean_ics_text("FGCU Soccer\\, Home")


def test_visitfortmyers_keeps_multi_day_still_running(monkeypatch):
    from events_sources import visitfortmyers as vfm

    today = __import__("datetime").date.today()
    started = (today.replace(day=1) if today.day > 1 else today).isoformat()
    # End a week from today so the listing is still active.
    end = (today.toordinal() + 7)
    end_s = __import__("datetime").date.fromordinal(end).isoformat()

    rss = """<?xml version="1.0"?>
    <rss version="2.0"><channel>
      <item><title>Running Show</title>
        <link>https://www.visitfortmyers.com/event/running-show/9999</link></item>
      <item><title>Expired Show</title>
        <link>https://www.visitfortmyers.com/event/expired-show/8888</link></item>
    </channel></rss>"""

    details = {
        "https://www.visitfortmyers.com/event/running-show/9999": f"""
            <p class="address"><span class="address-line1">100 Main St</span>
            <span class="locality">Fort Myers</span></p>
            <var class="atc_date_start">{started} 19:30:00</var>
            <var class="atc_date_end">{end_s} 21:00:00</var>
        """,
        "https://www.visitfortmyers.com/event/expired-show/8888": """
            <p class="address"><span class="address-line1">100 Main St</span>
            <span class="locality">Fort Myers</span></p>
            <var class="atc_date_start">2020-01-01 19:30:00</var>
            <var class="atc_date_end">2020-01-02 21:00:00</var>
        """,
    }

    class _Resp:
        def __init__(self, text: str, status: int = 200):
            self.text = text
            self.status_code = status

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError("http error")

    def _get(url, **_kwargs):
        if "rss" in url:
            return _Resp(rss)
        for key, html in details.items():
            if key in url or url.rstrip("/") == key.rstrip("/"):
                return _Resp(html)
        return _Resp("", 404)

    monkeypatch.setattr(vfm.requests, "get", _get)
    events = vfm.fetch_visitfortmyers_events(limit=5)
    titles = {e["title"] for e in events}
    assert "Running Show" in titles
    assert "Expired Show" not in titles
    assert events[0]["source"] == "lee_county"


def test_manual_events_load_from_public_json():
    from events_sources.manual import fetch_manual_events

    events = fetch_manual_events()
    assert events, "community-events.json should have upcoming seed events"
    assert all(e["source"] == "manual" for e in events)
    assert any("Farmers Market" in e["title"] for e in events)
    assert all(e["start"][:10] >= __import__("datetime").date.today().isoformat() for e in events)


def test_aggregate_merges_lee_and_vfm(monkeypatch):
    from events_sources import aggregate

    def _one(source: str, title: str, start: str):
        return [
            make_event(
                id=f"{source}-1",
                title=title,
                start=start,
                end=start,
                all_day=False,
                venue="Estero",
                url="",
                category="community",
                source=source,
            )
        ]

    monkeypatch.setattr(aggregate, "fetch_esterotoday_events", lambda: [])
    monkeypatch.setattr(aggregate, "fetch_fgcu_events", lambda: [])
    monkeypatch.setattr(
        aggregate,
        "fetch_lee_county_events",
        lambda: _one("lee_county", "Lakes Park Walk", "2099-01-10T08:00:00"),
    )
    monkeypatch.setattr(
        aggregate,
        "fetch_visitfortmyers_events",
        lambda: _one("lee_county", "Fort Myers Show", "2099-01-11T19:00:00"),
    )
    monkeypatch.setattr(aggregate, "fetch_manual_events", lambda: [])

    merged = aggregate.collect_upcoming_events()
    assert len(merged) == 2
    assert {e["title"] for e in merged} == {"Lakes Park Walk", "Fort Myers Show"}
