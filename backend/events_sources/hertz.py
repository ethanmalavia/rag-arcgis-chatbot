"""Hertz Arena upcoming events (public listing page HTML).

Source: https://hertzarena.com/events-tickets/upcoming-events/
Soft-fails on network/parse errors so the rest of /api/events still works.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime
from html import unescape
from typing import Any

import requests

from events_sources.normalize import make_event, map_category

logger = logging.getLogger(__name__)

LISTING_URL = "https://hertzarena.com/events-tickets/upcoming-events/"
VENUE = "Hertz Arena, Estero"
REQUEST_TIMEOUT = 12
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}

_BUCKET_RE = re.compile(
    r'<div class="event-bucket grid-item">(.*?)</div>\s*</div>\s*</div>',
    re.DOTALL | re.IGNORECASE,
)
_TITLE_RE = re.compile(r"<h3>(.*?)</h3>", re.DOTALL | re.IGNORECASE)
_INFO_RE = re.compile(
    r'<div class="eb-info">(.*?)</div>',
    re.DOTALL | re.IGNORECASE,
)
_DETAILS_RE = re.compile(
    r'href="(https://hertzarena\.com/events/[^"]+)"[^>]*class="eb-d-link"'
    r'|class="eb-d-link"[^>]*href="(https://hertzarena\.com/events/[^"]+)"',
    re.IGNORECASE,
)
_DATE_TIME_RE = re.compile(
    r"(?P<mon>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+"
    r"(?P<day>\d{1,2})"
    r"(?:\s*[|]\s*|\s+)"
    r"(?P<time>\d{1,2}:\d{2}\s*[ap]m)?",
    re.IGNORECASE,
)
_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def _strip_html(text: str) -> str:
    cleaned = re.sub(r"<[^>]+>", " ", text)
    cleaned = unescape(cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _parse_wall_time(raw: str | None) -> tuple[int, int]:
    if not raw:
        return 19, 0
    try:
        t = datetime.strptime(raw.upper().replace(" ", ""), "%I:%M%p")
        return t.hour, t.minute
    except ValueError:
        return 19, 0


def _assign_years(parsed: list[dict[str, Any]], *, today: date) -> list[dict[str, Any]]:
    """Attach calendar years to month/day rows from an upcoming chronological list."""
    year = today.year
    last_md: tuple[int, int] | None = None
    out: list[dict[str, Any]] = []
    for row in parsed:
        month, day = row["month"], row["day"]
        md = (month, day)
        if last_md is not None and md < last_md:
            year += 1
        try:
            d = date(year, month, day)
        except ValueError:
            continue
        if d < today:
            year += 1
            try:
                d = date(year, month, day)
            except ValueError:
                continue
        hour, minute = _parse_wall_time(row.get("time"))
        start = datetime(d.year, d.month, d.day, hour, minute).strftime("%Y-%m-%dT%H:%M:%S")
        out.append({**row, "start": start})
        last_md = md
    return out


def _parse_listing_html(html: str, *, today: date | None = None) -> list[dict[str, Any]]:
    today = today or date.today()
    rows: list[dict[str, Any]] = []
    for bucket in _BUCKET_RE.findall(html):
        title_m = _TITLE_RE.search(bucket)
        info_m = _INFO_RE.search(bucket)
        if not title_m or not info_m:
            continue
        title = _strip_html(title_m.group(1))
        info = _strip_html(info_m.group(1))
        dt_m = _DATE_TIME_RE.search(info)
        if not title or not dt_m:
            continue
        month = _MONTHS.get(dt_m.group("mon").lower()[:3])
        if not month:
            continue
        day = int(dt_m.group("day"))
        details = _DETAILS_RE.search(bucket)
        url = ""
        if details:
            url = details.group(1) or details.group(2) or ""
        rows.append(
            {
                "title": title,
                "month": month,
                "day": day,
                "time": (dt_m.group("time") or "").strip() or None,
                "url": url or LISTING_URL,
            }
        )

    dated = _assign_years(rows, today=today)
    events: list[dict[str, Any]] = []
    for row in dated:
        category = map_category(title=row["title"], default="music")
        # Wrestling / arena sports stay out of the music chip when keywords miss.
        low = row["title"].lower()
        if any(k in low for k in ("wwe", "wrestling", "hockey", "everblades")):
            category = "sports"
        key = f"{row['title'].lower()}|{row['start'][:10]}"
        events.append(
            make_event(
                id=f"hertz-{abs(hash(key)) % 10_000_000}",
                title=row["title"],
                start=row["start"],
                end=row["start"],
                all_day=False,
                venue=VENUE,
                url=row["url"],
                category=category,
                source="venue",
                location_tag="estero",
            )
        )
    return events


def fetch_hertz_events(*, limit: int = 20) -> list[dict[str, Any]]:
    """Scrape Hertz Arena upcoming-events listing. Soft-fails to []."""
    try:
        resp = requests.get(LISTING_URL, headers=_HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        events = _parse_listing_html(resp.text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Hertz Arena listing fetch failed: %s", exc)
        return []

    events.sort(key=lambda ev: ev.get("start") or "")
    out = events[: max(1, min(limit, 40))]
    logger.info("hertz events kept=%s", len(out))
    return out
