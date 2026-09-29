"""Merge EsteroToday + FGCU + Hertz + Visit Fort Myers + Lee County Parks + manual."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from events_sources.esterotoday import fetch_esterotoday_events
from events_sources.fgcu import fetch_fgcu_events
from events_sources.hertz import fetch_hertz_events
from events_sources.lee_county import fetch_lee_county_events
from events_sources.manual import fetch_manual_events
from events_sources.normalize import dedupe_events, sanitize_category
from events_sources.visitfortmyers import fetch_visitfortmyers_events

logger = logging.getLogger(__name__)

# Soft caps keep one noisy feed from burying Village / community listings.
_SOURCE_CAPS = {
    "esterotoday": 40,
    "venue": 30,  # FGCU athletics + Hertz Arena share this bucket
    "manual": 40,
    "lee_county": 25,
}


def _cap_source(events: list[dict[str, Any]], source: str) -> list[dict[str, Any]]:
    limit = _SOURCE_CAPS.get(source, 40)
    return events[:limit]


def collect_upcoming_events() -> list[dict[str, Any]]:
    """Fetch all sources, dedupe, sort ascending by start.

    Sources are fetched concurrently (a cold fetch is bounded by the slowest
    site, not the sum of all of them); each group still soft-fails on its own
    and merges in the same order as before.
    """
    with ThreadPoolExecutor(max_workers=6, thread_name_prefix="events") as pool:
        futures = {
            name: pool.submit(fn)
            for name, fn in (
                ("esterotoday", fetch_esterotoday_events),
                ("fgcu", fetch_fgcu_events),
                ("hertz", fetch_hertz_events),
                ("lee_county", fetch_lee_county_events),
                ("visitfortmyers", fetch_visitfortmyers_events),
                ("manual", fetch_manual_events),
            )
        }

    merged: list[dict[str, Any]] = []

    try:
        merged.extend(_cap_source(futures["esterotoday"].result(), "esterotoday"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("EsteroToday source failed: %s", exc)

    try:
        venue_batch: list[dict[str, Any]] = []
        venue_batch.extend(futures["fgcu"].result())
        venue_batch.extend(futures["hertz"].result())
        venue_batch.sort(key=lambda ev: ev.get("start") or "")
        merged.extend(_cap_source(venue_batch, "venue"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Venue sources (FGCU / Hertz) failed: %s", exc)

    try:
        # Tourism + parks both use source=lee_county for Pulse provenance.
        lee_batch: list[dict[str, Any]] = []
        lee_batch.extend(futures["lee_county"].result())
        lee_batch.extend(futures["visitfortmyers"].result())
        lee_batch.sort(key=lambda ev: ev.get("start") or "")
        merged.extend(_cap_source(lee_batch, "lee_county"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Lee County / Visit Fort Myers sources failed: %s", exc)

    try:
        merged.extend(_cap_source(futures["manual"].result(), "manual"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Manual events source failed: %s", exc)

    for ev in merged:
        ev["category"] = sanitize_category(ev.get("category"))

    merged = dedupe_events(merged)
    merged.sort(key=lambda ev: ev.get("start") or "")
    return merged
