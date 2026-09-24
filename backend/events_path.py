"""Structured answers for upcoming community-event questions."""
from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta

import pandas as pd

from events import list_upcoming_events
from models import ChatResponse, RouteKind

EVENTS_INTENT_RE = re.compile(
    r"\b("
    r"what'?s\s+happening|what\s+is\s+happening|whats\s+happening|"
    r"upcoming\s+events?|events?\s+this\s+(week|weekend|month)|"
    r"this\s+weekend|things\s+to\s+do|"
    r"concerts?|flea\s+markets?|farmers?\s+markets?|state\s+fairs?|"
    r"county\s+fairs?|sports?\s+events?|games?\s+(this|on)|"
    r"musical\s+performances?|live\s+music|"
    r"community\s+calendar|what'?s\s+on"
    r")\b",
    re.IGNORECASE,
)

_WEEKEND_RE = re.compile(r"\bweekend\b", re.IGNORECASE)
_WEEK_RE = re.compile(r"\b(this\s+)?week\b", re.IGNORECASE)
_SPORTS_RE = re.compile(r"\b(sports?|game|fgcu|soccer|basketball|football)\b", re.IGNORECASE)
_MUSIC_RE = re.compile(r"\b(concert|music|musical|performance)\b", re.IGNORECASE)
_MARKET_RE = re.compile(r"\b(flea\s+market|farmers?\s+market|market)\b", re.IGNORECASE)
_FAIR_RE = re.compile(r"\b(fair|carnival|expo)\b", re.IGNORECASE)


# A planning/zoning signal means the question is about the record, not the
# calendar — "what is happening on Corkscrew Road" is a RAG query that merely
# opens with an events-sounding phrase. Without this veto the events route
# short-circuits before the router and answers every such question with the
# same five upcoming events.
PLANNING_SIGNAL_RE = re.compile(
    r"\b("
    r"road|rd|street|st|avenue|ave|drive|dr|lane|ln|court|ct|"
    r"boulevard|blvd|parkway|pkwy|way|trail|trl|highway|hwy|"
    r"zoning|rezone|rezoned|rezoning|plat|replat|variance|easement|"
    r"ordinance|resolution|amendment|comprehensive\s+plan|"
    r"council|board|commission|hearing|agenda|minutes|"
    r"permit|development|developer|parcel|strap|district|"
    r"annexation|setback|density|acreage|subdivision|"
    r"interstate|i-?\s?75|us-?\s?41|sr-?\s?82|bridge|interchange|expansion|"
    r"widening|construction|traffic|infrastructure|"
    r"\d{4}"
    r")\b",
    re.IGNORECASE,
)

# A bare "what is happening" followed by "to/at/on/with <topic>" asks about that
# topic ("What is happening to I-75"), not about the community calendar — unless
# the rest of the question is itself calendar-shaped ("…at the park this weekend").
_TOPIC_HAPPENING_RE = re.compile(
    r"\bwhat(?:'?s|\s+is|\s+are)?\s+happening\s+(?:to|at|on|with|about|near|regarding)\s+\S+",
    re.IGNORECASE,
)
_CALENDAR_WORDS_RE = re.compile(
    r"\b(today|tonight|tomorrow|weekend|week|month|this\s+\w+day|monday|tuesday|wednesday|"
    r"thursday|friday|saturday|sunday|events?|calendar|schedule|festival|market|concert|"
    r"game|show|music|fair)\b",
    re.IGNORECASE,
)


_MONTHS = (
    "january|february|march|april|may|june|july|august|september|october|november|december"
)
_WEEKDAYS = "monday|tuesday|wednesday|thursday|friday|saturday|sunday"

# "events" (or "things to do") plus any time period is a calendar question no
# matter how it is phrased — "events in October", "any events tomorrow",
# "events next week", "events on Saturday".
_EVENT_WORD_RE = re.compile(r"\b(?:events?|things\s+to\s+do)\b", re.IGNORECASE)
_TIME_PERIOD_RE = re.compile(
    r"\b(?:today|tonight|tomorrow|weekends?|"
    r"(?:this|next|the\s+coming|the\s+upcoming|coming|upcoming)\s+"
    r"(?:week|weekend|month|evening|afternoon|morning|few\s+days|few\s+weeks|days|weeks)|"
    r"next\s+(?:" + _WEEKDAYS + r")|(?:on|this)\s+(?:" + _WEEKDAYS + r")s?|"
    r"(?:in|during|for)\s+(?:" + _MONTHS + r")|(?:" + _MONTHS + r")\s+\d{1,2}|"
    r"(?:(?:in|over|within|for)\s+)?the\s+next\s+\w+\s+(?:days?|weeks?|months?)|"
    r"this\s+year|"
    r"this\s+(?:coming\s+)?(?:" + _WEEKDAYS + r"))\b",
    re.IGNORECASE,
)
# Even with a time period, zoning-record vocabulary means a records question.
_STRICT_PLANNING_RE = re.compile(
    r"\b(?:zoning|rezon\w*|ordinance|resolution|variance|hearing|agenda|minutes|"
    r"comprehensive\s+plan|development\s+order|plat|easement)\b",
    re.IGNORECASE,
)

# Event/time words say nothing about *which* record a question names — they
# just happen to appear in old meeting summaries ("upcoming events", "weekly").
_EVENTS_VETO_STOPWORDS = frozenset(
    {"the", "and", "for", "what", "show", "minutes", "meeting", "estero", "village",
     "happening", "going", "with", "about", "this", "that", "there", "any",
     "upcoming", "events", "event", "week", "weekend", "weekends", "month", "today",
     "tonight", "tomorrow", "next", "coming", "days", "evening", "afternoon", "morning",
     "things", "are", "there", "have", "has", "list", "show", "tell", "give", "please"}
    | set(_MONTHS.split("|")) | set(_WEEKDAYS.split("|"))
)


def _matches_named_record(df: pd.DataFrame, question: str) -> bool:
    """True only when a content token from the question literally appears in
    a project/location/id column — never "everything matches" on no tokens.

    Deliberately not keyword_path.answer_keyword(): that function falls back
    to returning the *entire* dataframe when no token matches anything (a
    reasonable UX default for its own keyword-shortcut route), which would
    make this veto fire on every question once the dataset is non-empty.
    """
    from schema_aliases import search_columns

    tokens = [t for t in re.findall(r"[a-z0-9]{3,}", question.lower()) if t not in _EVENTS_VETO_STOPWORDS]
    if not tokens:
        return False
    for col in search_columns(df):
        series = df[col].astype(str)
        if any(series.str.contains(tok, case=False, na=False).any() for tok in tokens):
            return True
    return False


def is_events_question(question: str, df: pd.DataFrame | None = None) -> bool:
    q = question or ""
    if _EVENT_WORD_RE.search(q) and _TIME_PERIOD_RE.search(q) and not _STRICT_PLANNING_RE.search(q):
        return True
    if not EVENTS_INTENT_RE.search(q):
        return False
    # Planning language wins: fall through to the router and RAG.
    if PLANNING_SIGNAL_RE.search(q):
        return False
    if _TOPIC_HAPPENING_RE.search(q) and not _CALENDAR_WORDS_RE.search(q):
        return False
    # PLANNING_SIGNAL_RE only catches street-suffix/zoning jargon — a bare
    # named project/business ("What is happening at Wawa") has neither, so it
    # would otherwise short-circuit here and answer with the generic upcoming
    # events list. If the dataset actually names the thing being asked about,
    # it's a specific record, not the community calendar.
    if df is not None and not df.empty and _matches_named_record(df, q):
        return False
    return True


def _parse_day(start: str) -> date | None:
    try:
        return date.fromisoformat(start[:10])
    except ValueError:
        return None


def _format_when(ev: dict) -> str:
    start = ev.get("start") or ""
    d = _parse_day(start)
    if not d:
        return start[:16]
    label = d.strftime("%a %b ") + str(d.day)
    if ev.get("allDay"):
        return f"{label} (all day)"
    try:
        dt = datetime.fromisoformat(start)
        hour = dt.strftime("%I").lstrip("0") or "12"
        return f"{label} {hour}:{dt.strftime('%M %p')}"
    except ValueError:
        return label


_NUM_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "ten": 10, "a": 1, "one": 1}
_MONTH_NUM = {m: i for i, m in enumerate(_MONTHS.split("|"), start=1)}
_WEEKDAY_NUM = {d: i for i, d in enumerate(_WEEKDAYS.split("|"))}
_MONTH_NAME_RE = re.compile(r"\b(" + _MONTHS + r")\b", re.IGNORECASE)
_WEEKDAY_NAME_RE = re.compile(r"\b(" + _WEEKDAYS + r")s?\b", re.IGNORECASE)


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def _window_for_question(question: str) -> tuple[date, date]:
    today = date.today()
    q = (question or "").lower()
    if re.search(r"\b(?:today|tonight)\b", q):
        return today, today
    if re.search(r"\btomorrow\b", q):
        return today + timedelta(days=1), today + timedelta(days=1)
    m = _WEEKDAY_NAME_RE.search(q)
    if m and re.search(r"\b(?:on|this|next)\s+" + m.group(1), q):
        ahead = (_WEEKDAY_NUM[m.group(1).lower()] - today.weekday()) % 7
        if re.search(r"\bnext\s+" + m.group(1), q) and ahead < 7:
            ahead += 7 if ahead == 0 else 0
        day = today + timedelta(days=ahead)
        return day, day
    if re.search(r"\bnext\s+month\b", q):
        y, mo = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
        return _month_bounds(y, mo)
    if re.search(r"\bthis\s+month\b", q):
        return today, _month_bounds(today.year, today.month)[1]
    m = _MONTH_NAME_RE.search(q)
    if m:
        mo = _MONTH_NUM[m.group(1).lower()]
        year = today.year if mo >= today.month else today.year + 1
        dm = re.search(m.group(1) + r"\s+(\d{1,2})\b", q)
        if dm:
            try:
                day = date(year, mo, int(dm.group(1)))
                return day, day
            except ValueError:
                pass
        if re.search(r"\b(?:in|during|for)\s+" + m.group(1), q):
            first, last = _month_bounds(year, mo)
            return max(first, today), last
    if re.search(r"\bnext\s+weekend\b", q):
        wd = today.weekday()
        sat = today + timedelta(days=(5 - wd) % 7)
        start = sat if wd == 6 else sat + timedelta(days=7)
        return start, start + timedelta(days=1)
    if re.search(r"\bthis\s+year\b", q):
        return today, date(today.year, 12, 31)
    nm = re.search(r"\bnext\s+(\d+|" + "|".join(_NUM_WORDS) + r")\s+(days?|weeks?|months?)\b", q)
    if nm:
        n = int(nm.group(1)) if nm.group(1).isdigit() else _NUM_WORDS[nm.group(1)]
        unit = {"d": 1, "w": 7, "m": 30}[nm.group(2)[0]]
        return today, today + timedelta(days=n * unit)
    if _WEEKEND_RE.search(question):
        weekday = today.weekday()  # Mon=0 … Sun=6
        if weekday == 5:
            return today, today + timedelta(days=1)
        if weekday == 6:
            return today, today
        days_until_sat = 5 - weekday
        start = today + timedelta(days=days_until_sat)
        return start, start + timedelta(days=1)
    if re.search(r"\bnext\s+week\b", q):
        start = today + timedelta(days=7 - today.weekday())
        return start, start + timedelta(days=6)
    if _WEEK_RE.search(question):
        return today, today + timedelta(days=7)
    return today, today + timedelta(days=21)


def _category_filter(question: str) -> set[str] | None:
    cats: set[str] = set()
    if _SPORTS_RE.search(question):
        cats.add("sports")
    if _MUSIC_RE.search(question):
        cats.add("music")
    if _MARKET_RE.search(question):
        cats.add("market")
    if _FAIR_RE.search(question):
        cats.add("fair")
    return cats or None


def answer_upcoming_events(question: str) -> ChatResponse:
    events = list_upcoming_events()
    start_day, end_day = _window_for_question(question)
    cats = _category_filter(question)

    filtered: list[dict] = []
    for ev in events:
        d = _parse_day(str(ev.get("start") or ""))
        if d is None or d < start_day or d > end_day:
            continue
        if cats and ev.get("category") not in cats:
            continue
        filtered.append(ev)

    filtered = filtered[:5]
    if not filtered:
        summary = (
            "- I don't have upcoming community events in that window.\n"
            "- Check esterotoday.com/events for the full local calendar."
        )
        return ChatResponse(
            summary=summary,
            answer=summary,
            projects=[],
            route=RouteKind.EVENTS.value,
            meta={"llm_skipped": True, "paths": ["events"], "events_count": 0},
        )

    bullets: list[str] = []
    for ev in filtered:
        when = _format_when(ev)
        venue = ev.get("venue") or "Estero area"
        title = ev.get("title") or "Event"
        url = ev.get("url") or ""
        if url:
            bullets.append(f"- {when}: {title} at {venue} — {url}")
        else:
            bullets.append(f"- {when}: {title} at {venue}.")

    summary = "\n".join(bullets)
    return ChatResponse(
        summary=summary,
        answer=summary,
        projects=[],
        route=RouteKind.EVENTS.value,
        meta={
            "llm_skipped": True,
            "paths": ["events"],
            "events_count": len(filtered),
            "window": {"start": start_day.isoformat(), "end": end_day.isoformat()},
        },
    )
