"""Upcoming Events widget — aggregated Estero-area community calendar.

Sources (see backend/events_sources/):
- EsteroToday The Events Calendar REST API (all categories)
- FGCU Athletics ICS (local Fort Myers / FGCU games)
- Hertz Arena upcoming-events listing (HTML)
- Lee County Parks & Recreation calendar (HTML)
- Visit Fort Myers tourism RSS + event detail pages
- frontend-react/public/community-events.json manual fallback

Fetched server-side so the frontend never needs CORS handling for third-party
hosts, and so esterotoday.com's WAF quirk (browser-like User-Agent required)
lives in one place.
"""
from __future__ import annotations

import time

from fastapi import APIRouter

from events_sources import collect_upcoming_events
from events_sources.normalize import load_event_config

router = APIRouter()

CACHE_TTL_SECONDS = 30 * 60
_cache: dict = {"events": None, "fetched_at": 0.0}


@router.get("/api/events")
def get_events() -> dict:
    """Merged, deduped, future-only community events. Cached in-process for
    CACHE_TTL_SECONDS; on refresh failure, serves the last good cache."""
    now = time.time()
    cache_fresh = _cache["events"] is not None and (now - _cache["fetched_at"]) < CACHE_TTL_SECONDS
    if cache_fresh:
        return {"events": _cache["events"], "error": False, "meta": _meta()}

    try:
        events = collect_upcoming_events()
        _cache["events"] = events
        _cache["fetched_at"] = now
        return {"events": events, "error": False, "meta": _meta()}
    except Exception as exc:  # noqa: BLE001 - degrade gracefully
        print(f"Warning: /api/events fetch failed: {exc}")
        if _cache["events"] is not None:
            return {"events": _cache["events"], "error": False, "meta": _meta()}
        return {"events": [], "error": True, "meta": _meta()}


def _meta() -> dict:
    cfg = load_event_config()
    return {
        "chip_order": cfg.get("chip_order") or [],
        "chip_labels": cfg.get("chip_labels") or {},
        "sources": ["esterotoday", "venue", "lee_county", "manual"],
    }


def list_upcoming_events(*, force_refresh: bool = False) -> list[dict]:
    """Internal helper for chat / tests — returns the cached or live list."""
    if force_refresh:
        _cache["events"] = None
        _cache["fetched_at"] = 0.0
    payload = get_events()
    return list(payload.get("events") or [])
