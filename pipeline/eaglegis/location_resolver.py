"""Typed location resolution.

Every agenda-item location reference is classified into one of a small set of
location_types and resolved to **exactly one** (latitude, longitude) point. The
resolution strategy varies by type, but the output schema does not — the
frontend still consumes points.

Types:
- PARCEL_ADDRESS    : "10800 Corkscrew Road" — Lee County parcel centroid.
- MULTI_PARCEL      : "10170 and 10150 Arcos Avenue" — average of both parcel centroids.
- INTERSECTION      : "Corkscrew Road & Puente Lane" — endpoint of the segment that touches both streets.
- CORRIDOR          : "Estero Parkway from US 41 to Three Oaks" — midpoint of the road segments between the two named endpoints.
- WHOLE_STREET      : "along Estero Parkway" — midpoint of all centerline segments for the street.
- NAMED_VENUE       : "Estero Village Hall", "Hertz Arena" — known canonical point.
- NEIGHBORHOOD      : "Pelican Sound", "Estero Bay Village" — centroid of community polygon.
- ANCHORED_OFFSET   : "645 feet south of Salerno Bay Road" — anchor point + bearing + distance.
- OVERRIDE_TEXT     : pinned via SITE_TEXT_LOCATION_OVERRIDES / SITE_LOCATION_OVERRIDES.

All resolvers are wrapped in a disk cache so re-runs are near-instant.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterable


# ---------------------------------------------------------------------------
# Constants and external services
# ---------------------------------------------------------------------------

PARCEL_URL = (
    "https://services2.arcgis.com/LvWGAAhHwbCJ2GMP/arcgis/rest/services/"
    "Lee_County_Parcels/FeatureServer/0/query"
)
ROAD_URL = (
    "https://services2.arcgis.com/LvWGAAhHwbCJ2GMP/arcgis/rest/services/"
    "RoadCenterline/FeatureServer/0/query"
)
NEIGHBORHOOD_URL = (
    "https://services2.arcgis.com/LvWGAAhHwbCJ2GMP/arcgis/rest/services/"
    "Neighborhoods_and_Areas/FeatureServer/0/query"
)
PARK_URL = (
    "https://services2.arcgis.com/LvWGAAhHwbCJ2GMP/arcgis/rest/services/"
    "Park_Locations/FeatureServer/0/query"
)

HTTP_TIMEOUT_S = 30
PAUSE_BETWEEN_CALLS_S = 0.03

# Estero is in Lee County south of Fort Myers — restrict queries to a rough
# bounding box so we don't pull centerlines from the whole county.
ESTERO_BBOX_LATLON = {  # (min_lon, min_lat, max_lon, max_lat)
    "min_lon": -81.86, "min_lat": 26.34,
    "max_lon": -81.70, "max_lat": 26.50,
}
# Tighter "core Estero" box used when collapsing a corridor or whole-street
# reference to a single point — keeps the midpoint of long roads (Corkscrew,
# US 41) anchored inside the Village proper instead of drifting east into
# rural Lee County.
ESTERO_CORE_BBOX = {
    "min_lon": -81.84, "min_lat": 26.36,
    "max_lon": -81.74, "max_lat": 26.47,
}


def _point_in_core(lon: float, lat: float) -> bool:
    b = ESTERO_CORE_BBOX
    return b["min_lon"] <= lon <= b["max_lon"] and b["min_lat"] <= lat <= b["max_lat"]

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


STREET_SUFFIX_SYNONYMS = {
    "road": "RD", "rd": "RD",
    "street": "ST", "st": "ST",
    "avenue": "AVE", "ave": "AVE",
    "boulevard": "BLVD", "blvd": "BLVD",
    "drive": "DR", "dr": "DR",
    "lane": "LN", "ln": "LN",
    "court": "CT", "ct": "CT",
    "circle": "CIR", "cir": "CIR",
    "parkway": "PKWY", "pkwy": "PKWY",
    "trail": "TRL", "trl": "TRL",
    "place": "PL", "pl": "PL",
    "way": "WAY",
    "terrace": "TER", "ter": "TER",
    "highway": "HWY", "hwy": "HWY",
    "point": "PT", "pt": "PT",
}

DIRECTIONAL_WORDS = {
    "north": "N", "n": "N",
    "south": "S", "s": "S", "so": "S",
    "east": "E", "e": "E",
    "west": "W", "w": "W",
    "northeast": "NE", "ne": "NE",
    "northwest": "NW", "nw": "NW",
    "southeast": "SE", "se": "SE",
    "southwest": "SW", "sw": "SW",
}

# Known canonical aliases for streets where PDFs use a different spelling than
# the parcel/road layer.
STREET_ALIASES = {
    "US 41": ["S TAMIAMI TRL", "TAMIAMI TRL"],
    "U.S. 41": ["S TAMIAMI TRL", "TAMIAMI TRL"],
    "TAMIAMI TRAIL": ["S TAMIAMI TRL", "TAMIAMI TRL"],
    "S TAMIAMI TRAIL": ["S TAMIAMI TRL"],
    "SOUTH TAMIAMI TRAIL": ["S TAMIAMI TRL"],
    "BROADWAY AVENUE EAST": ["BROADWAY E"],
    "BROADWAY AVENUE WEST": ["BROADWAY W"],
    "BROADWAY EAST": ["BROADWAY E"],
    "BROADWAY WEST": ["BROADWAY W"],
}


# ---------------------------------------------------------------------------
# LocationReference value object
# ---------------------------------------------------------------------------

@dataclass
class LocationReference:
    """One agenda-item location, fully resolved to a single point."""
    location_type: str
    raw_text: str
    latitude: float
    longitude: float
    confidence: float
    address_label: str = ""
    resolution_notes: str = ""
    parcel_strap: str = ""
    feature_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Disk-cached HTTP helper
# ---------------------------------------------------------------------------

class _CachedRequester:
    def __init__(self, cache_path: Path) -> None:
        self.cache_path = cache_path
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache: dict[str, Any] = {}
        if cache_path.exists():
            try:
                self.cache = json.loads(cache_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self.cache = {}
        self.calls = 0
        self.hits = 0
        self._dirty = False

    def fetch(self, key: str, url: str, params: dict[str, str]) -> Any:
        if key in self.cache:
            self.hits += 1
            return self.cache[key]
        full = url + "?" + urllib.parse.urlencode(params)
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(full, context=_SSL_CTX, timeout=HTTP_TIMEOUT_S) as r:
                    data = json.load(r)
                self.cache[key] = data
                self._dirty = True
                self.calls += 1
                time.sleep(PAUSE_BETWEEN_CALLS_S)
                return data
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
                last_err = e
                time.sleep(0.5 * (attempt + 1))
        raise RuntimeError(f"HTTP query failed after retries: {last_err}")

    def flush(self) -> None:
        if not self._dirty:
            return
        self.cache_path.write_text(
            json.dumps(self.cache, indent=2, sort_keys=True), encoding="utf-8"
        )
        self._dirty = False


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Distance between two (lon, lat) points in metres."""
    lon1, lat1 = a
    lon2, lat2 = b
    r = 6371000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def polyline_length_m(path: list[list[float]]) -> float:
    if len(path) < 2:
        return 0.0
    total = 0.0
    for a, b in zip(path[:-1], path[1:]):
        total += haversine_m((a[0], a[1]), (b[0], b[1]))
    return total


def polyline_midpoint(path: list[list[float]]) -> tuple[float, float] | None:
    """Return the point at the midpoint by length along a single polyline path."""
    if not path:
        return None
    if len(path) == 1:
        return (path[0][0], path[0][1])
    half = polyline_length_m(path) / 2.0
    accumulated = 0.0
    for a, b in zip(path[:-1], path[1:]):
        seg_len = haversine_m((a[0], a[1]), (b[0], b[1]))
        if accumulated + seg_len >= half:
            if seg_len == 0:
                return (a[0], a[1])
            t = (half - accumulated) / seg_len
            lon = a[0] + t * (b[0] - a[0])
            lat = a[1] + t * (b[1] - a[1])
            return (lon, lat)
        accumulated += seg_len
    last = path[-1]
    return (last[0], last[1])


def best_midpoint_across_paths(
    paths: list[list[list[float]]],
    prefer_core: bool = False,
) -> tuple[float, float] | None:
    """Return a single representative point for a set of polyline paths.

    For corridors / whole-street references, callers want one "middle of the
    road" pin. We compute it as the centroid of each segment's midpoint,
    weighted by segment length — so a long road with many short segments
    near one end (typical in dense neighborhoods) still anchors near the
    geographic center, not at the edge of the bbox.

    When ``prefer_core`` is True, candidate segments are first filtered to
    those whose midpoint sits inside the tight ESTERO_CORE_BBOX. If none
    qualify, we fall back to the full set.
    """
    if not paths:
        return None
    candidate_paths = paths
    if prefer_core:
        in_core: list[list[list[float]]] = []
        for path in paths:
            mid = polyline_midpoint(path)
            if mid and _point_in_core(mid[0], mid[1]):
                in_core.append(path)
        if in_core:
            candidate_paths = in_core

    weighted_sum_lon = 0.0
    weighted_sum_lat = 0.0
    total_weight = 0.0
    for path in candidate_paths:
        mid = polyline_midpoint(path)
        if mid is None:
            continue
        weight = max(polyline_length_m(path), 1.0)
        weighted_sum_lon += mid[0] * weight
        weighted_sum_lat += mid[1] * weight
        total_weight += weight
    if total_weight == 0:
        return polyline_midpoint(candidate_paths[0]) if candidate_paths else None
    return (weighted_sum_lon / total_weight, weighted_sum_lat / total_weight)


def average_point(points: Iterable[tuple[float, float]]) -> tuple[float, float] | None:
    pts = list(points)
    if not pts:
        return None
    lon = sum(p[0] for p in pts) / len(pts)
    lat = sum(p[1] for p in pts) / len(pts)
    return (lon, lat)


def offset_point(
    origin: tuple[float, float],
    bearing_deg: float,
    distance_m: float,
) -> tuple[float, float]:
    """Return the point distance_m from origin along bearing (degrees from N)."""
    r = 6371000.0
    lon1, lat1 = math.radians(origin[0]), math.radians(origin[1])
    b = math.radians(bearing_deg)
    d_r = distance_m / r
    lat2 = math.asin(
        math.sin(lat1) * math.cos(d_r)
        + math.cos(lat1) * math.sin(d_r) * math.cos(b)
    )
    lon2 = lon1 + math.atan2(
        math.sin(b) * math.sin(d_r) * math.cos(lat1),
        math.cos(d_r) - math.sin(lat1) * math.sin(lat2),
    )
    return (math.degrees(lon2), math.degrees(lat2))


DIRECTION_TO_BEARING = {
    "N": 0, "NE": 45, "E": 90, "SE": 135,
    "S": 180, "SW": 225, "W": 270, "NW": 315,
}


# ---------------------------------------------------------------------------
# Street-text normalization
# ---------------------------------------------------------------------------

def trim_action_prefix(name: str) -> str:
    """Drop leading words that are common municipal action verbs.

    Regex captures like "Repave Three Oaks Parkway" should yield "Three Oaks
    Parkway" for street lookups.
    """
    tokens = name.strip().rstrip(".,;:)").split()
    while tokens and tokens[0].lower() in ACTION_PREFIX_WORDS:
        tokens = tokens[1:]
    return " ".join(tokens)


def parse_street_parts(text: str) -> tuple[str, str, str] | None:
    """Split a street name into (base_words, suffix_abbr, trailing_dir).

    Returns None if no recognisable street suffix is present.
    Examples:
      "Estero Parkway"           → ("ESTERO", "PKWY", "")
      "S. Tamiami Trail"         → ("S TAMIAMI", "TRL", "")
      "Broadway Avenue East"     → ("BROADWAY", "AVE", "E")
      "Murano Del Lago Drive"    → ("MURANO DEL LAGO", "DR", "")
    """
    tokens = [t.rstrip(".") for t in text.split() if t.rstrip(".")]
    if not tokens:
        return None
    trailing_dir = ""
    if len(tokens) >= 2 and tokens[-1].lower() in DIRECTIONAL_WORDS:
        trailing_dir = DIRECTIONAL_WORDS[tokens[-1].lower()]
        tokens = tokens[:-1]
    suffix_abbr = ""
    if tokens and tokens[-1].lower() in STREET_SUFFIX_SYNONYMS:
        suffix_abbr = STREET_SUFFIX_SYNONYMS[tokens[-1].lower()]
        tokens = tokens[:-1]
    if not trailing_dir and len(tokens) >= 2 and tokens[-1].lower() in DIRECTIONAL_WORDS:
        trailing_dir = DIRECTIONAL_WORDS[tokens[-1].lower()]
        tokens = tokens[:-1]
    if tokens and tokens[0].lower() in DIRECTIONAL_WORDS:
        tokens[0] = DIRECTIONAL_WORDS[tokens[0].lower()]
    if not tokens:
        return None
    base = re.sub(r"[^A-Z0-9 ]", "", " ".join(t.upper() for t in tokens)).strip()
    if not base:
        return None
    return base, suffix_abbr, trailing_dir


def normalize_street_core(text: str) -> str:
    """Backwards-compatible: return base name only (no suffix, no trailing dir)."""
    parts = parse_street_parts(text)
    return parts[0] if parts else ""


def street_search_variants(name: str) -> list[str]:
    """Variants to probe in SITESTREET / STREET LIKE clauses, most specific first.

    The parcel layer is inconsistent — "MURANO DEL LAGO DR" keeps the DR suffix,
    "BROADWAY E" drops the AVE suffix — so we build several variants and let
    the caller try each.
    """
    parts = parse_street_parts(name)
    if not parts:
        return []
    base, suffix, direction = parts
    candidates: list[str] = []

    def add(v: str) -> None:
        v = v.strip()
        if v and v not in candidates:
            candidates.append(v)

    # Most specific: base + suffix + direction
    if suffix and direction:
        add(f"{base} {suffix} {direction}")
    if direction:
        add(f"{base} {direction}")
    if suffix:
        add(f"{base} {suffix}")
    add(base)

    alias_list = STREET_ALIASES.get(name.upper()) or STREET_ALIASES.get(base)
    if alias_list:
        for a in alias_list:
            add(a)
    return candidates


# ---------------------------------------------------------------------------
# Service clients (parcels, roads, neighborhoods, parks)
# ---------------------------------------------------------------------------

class ParcelClient:
    OUT_FIELDS = "STRAP,SITEADDR,SITENUMBER,SITESTREET,SITECITY,SITEZIP"

    def __init__(self, requester: _CachedRequester) -> None:
        self.r = requester

    def parcel_at_point(self, lon: float, lat: float) -> list[dict]:
        key = f"parcel.pt:{lon:.6f},{lat:.6f}"
        params = {
            "geometry": f"{lon},{lat}",
            "geometryType": "esriGeometryPoint",
            "inSR": "4326", "outSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": self.OUT_FIELDS,
            "returnGeometry": "false", "f": "json",
        }
        data = self.r.fetch(key, PARCEL_URL, params)
        return [ft.get("attributes", {}) for ft in data.get("features", [])]

    def parcels_at_address(self, number: str, street_core: str) -> list[dict]:
        # Bounded to the Estero envelope: a mis-parsed number must not match
        # a same-named street elsewhere in Lee County (e.g. "15 Broadway Cir"
        # in Fort Myers). Key prefix "estero|" invalidates older unbounded
        # cache entries.
        key = f"parcel.addr:estero|{number}|{street_core}"
        bbox = ESTERO_BBOX_LATLON
        where = f"SITENUMBER='{number}' AND UPPER(SITESTREET) LIKE '%{street_core}%'"
        params = {
            "where": where,
            "outFields": self.OUT_FIELDS,
            "returnGeometry": "false",
            "returnCentroid": "true",
            "outSR": "4326", "f": "json",
            "geometry": f"{bbox['min_lon']},{bbox['min_lat']},{bbox['max_lon']},{bbox['max_lat']}",
            "geometryType": "esriGeometryEnvelope",
            "spatialRel": "esriSpatialRelIntersects",
            "inSR": "4326",
        }
        data = self.r.fetch(key, PARCEL_URL, params)
        out: list[dict] = []
        for ft in data.get("features", []):
            a = dict(ft.get("attributes", {}))
            c = ft.get("centroid") or {}
            if c.get("x") is not None and c.get("y") is not None:
                a["_lon"] = c["x"]
                a["_lat"] = c["y"]
            out.append(a)
        return out


class RoadClient:
    OUT_FIELDS = "STREET,FROMSTREET,TOSTREET,FEET"

    def __init__(self, requester: _CachedRequester) -> None:
        self.r = requester

    def segments_for_street(self, street_core: str) -> list[dict]:
        """All centerline segments whose STREET contains street_core, in Estero bbox."""
        key = f"road.street:{street_core}"
        bbox = ESTERO_BBOX_LATLON
        # Exact-token match: surround with word boundaries so "ESTERO" doesn't
        # also pull "ESTERO BAY VILLAGE" or similar unrelated streets.
        where = (
            f"(UPPER(STREET) = '{street_core}' OR "
            f"UPPER(STREET) LIKE '{street_core} %' OR "
            f"UPPER(STREET) LIKE '% {street_core}' OR "
            f"UPPER(STREET) LIKE '% {street_core} %')"
        )
        params = {
            "where": where,
            "outFields": self.OUT_FIELDS,
            "returnGeometry": "true",
            "outSR": "4326", "f": "json",
            "geometry": f"{bbox['min_lon']},{bbox['min_lat']},{bbox['max_lon']},{bbox['max_lat']}",
            "geometryType": "esriGeometryEnvelope",
            "spatialRel": "esriSpatialRelIntersects",
            "inSR": "4326",
        }
        data = self.r.fetch(key, ROAD_URL, params)
        return data.get("features", [])

    def segments_for_street_variants(self, variants: list[str]) -> list[dict]:
        """Try each variant in order; return the first non-empty result."""
        for v in variants:
            segs = self.segments_for_street(v)
            if segs:
                return segs
        return []


class NeighborhoodClient:
    OUT_FIELDS = "name,descriptive_name,postal_city,type"

    def __init__(self, requester: _CachedRequester) -> None:
        self.r = requester

    def neighborhoods_by_name(self, name_core: str) -> list[dict]:
        key = f"neighbor:{name_core}"
        bbox = ESTERO_BBOX_LATLON
        where = (
            f"UPPER(name) LIKE '%{name_core}%' OR "
            f"UPPER(descriptive_name) LIKE '%{name_core}%'"
        )
        params = {
            "where": where,
            "outFields": self.OUT_FIELDS,
            "returnGeometry": "false",
            "returnCentroid": "true",
            "outSR": "4326", "f": "json",
            "geometry": f"{bbox['min_lon']},{bbox['min_lat']},{bbox['max_lon']},{bbox['max_lat']}",
            "geometryType": "esriGeometryEnvelope",
            "spatialRel": "esriSpatialRelIntersects",
            "inSR": "4326",
        }
        data = self.r.fetch(key, NEIGHBORHOOD_URL, params)
        out: list[dict] = []
        for ft in data.get("features", []):
            a = dict(ft.get("attributes", {}))
            c = ft.get("centroid") or {}
            if c.get("x") is not None and c.get("y") is not None:
                a["_lon"] = c["x"]
                a["_lat"] = c["y"]
            out.append(a)
        return out


class ParkClient:
    OUT_FIELDS = "Name,Category,Type"

    def __init__(self, requester: _CachedRequester) -> None:
        self.r = requester

    def parks_by_name(self, name_core: str) -> list[dict]:
        key = f"park:{name_core}"
        bbox = ESTERO_BBOX_LATLON
        where = f"UPPER(Name) LIKE '%{name_core}%'"
        params = {
            "where": where,
            "outFields": self.OUT_FIELDS,
            "returnGeometry": "true",
            "outSR": "4326", "f": "json",
            "geometry": f"{bbox['min_lon']},{bbox['min_lat']},{bbox['max_lon']},{bbox['max_lat']}",
            "geometryType": "esriGeometryEnvelope",
            "spatialRel": "esriSpatialRelIntersects",
            "inSR": "4326",
        }
        data = self.r.fetch(key, PARK_URL, params)
        out: list[dict] = []
        for ft in data.get("features", []):
            a = dict(ft.get("attributes", {}))
            g = ft.get("geometry") or {}
            if g.get("x") is not None and g.get("y") is not None:
                a["_lon"] = g["x"]
                a["_lat"] = g["y"]
            out.append(a)
        return out


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------

# Patterns matched against agenda item text.  Order matters — more specific
# first.

MULTI_ADDRESS_RE = re.compile(
    r"\b(?P<n1>\d{2,6})\s+(?:and|&)\s+(?P<n2>\d{2,6})\s+(?P<street>[A-Z][A-Za-z.'\-]+(?:\s+[A-Z][A-Za-z.'\-]+){0,5})\b",
)
SINGLE_ADDRESS_RE = re.compile(
    r"\b(?P<num>\d{3,6})\s+(?P<street>[A-Z][A-Za-z.'\-]+(?:\s+[A-Z][A-Za-z.'\-]+){0,5})\b",
)
# Matches "X, Y & Z Street" or "X & Y Street" (up to 5 numbers all sharing one
# street).  Requires the street to end in a recognised suffix so that
# "2024 East Wing" can't be parsed as "20, 24 East Wing".  Used by resolve_all
# to fan out genuinely multi-parcel references.
_MULTI_NUMS = (
    r"(?P<n1>\d{2,6})"
    r"(?:\s*(?:,|&|and)\s*(?P<n2>\d{2,6}))"
    r"(?:\s*(?:,|&|and)\s*(?P<n3>\d{2,6}))?"
    r"(?:\s*(?:,|&|and)\s*(?P<n4>\d{2,6}))?"
    r"(?:\s*(?:,|&|and)\s*(?P<n5>\d{2,6}))?"
)
_MULTI_STREET = (
    r"(?P<street>[A-Z][A-Za-z.'\-]+(?:\s+[A-Z][A-Za-z.'\-]+){0,4}\s+"
    r"(?:Road|Rd|Street|St|Avenue|Ave|Boulevard|Blvd|Drive|Dr|Lane|Ln|"
    r"Parkway|Pkwy|Trail|Trl|Highway|Hwy|Way|Circle|Cir|Court|Ct|"
    r"Terrace|Ter|Place|Pl|Point|Pt))"
)
# The leading lookbehind keeps the tail of a hyphenated reference number
# ("Ordinance No. 2025-15, 4741 Broadway Avenue West") from being read as a
# house number sharing the street with the real address.
COMPOUND_ADDRESS_RE = re.compile(rf"(?<![-–])\b{_MULTI_NUMS}\s+{_MULTI_STREET}")
# Immediately-preceding context that marks a number as a document/contract
# reference rather than a street number.
REFERENCE_NUMBER_CONTEXT_RE = re.compile(
    r"(?:[-–]\s*|\b(?:no|nos|number)\.?\s*|"
    r"\b(?:ordinance|resolution|bid|contract|rfb|rfq|rfp|cn|ec|sta)\s+)$",
    flags=re.I,
)


def _is_reference_number(text: str, position: int) -> bool:
    """True when the number starting at ``position`` follows ordinance/
    contract-style context and therefore is not a street number."""
    lookback = text[max(0, position - 14):position]
    return bool(REFERENCE_NUMBER_CONTEXT_RE.search(lookback))
# Phrases that signal the following street/landmark/address is descriptive
# context, not a separate site of the project.  Used to skip matches like:
#   "located south of Corkscrew Road and west of Via Coconut Point"
#   "approximately 1,000 feet west of Three Oaks Parkway"
#   "across from the Estero Health Center"
#   "north of the Fountain Lakes entrance"
# When DIRECTIONAL_CONTEXT_RE matches within ~80 chars BEFORE an address,
# that address is ignored by resolve_all's fan-out scan.
DIRECTIONAL_CONTEXT_RE = re.compile(
    r"(?:"
    r"(?:on\s+the\s+)?(?:north|south|east|west|northeast|northwest|southeast|southwest)(?:ern)?"
    r"\s+(?:side|corner|end)\s+of"
    r"|(?:north|south|east|west|northeast|northwest|southeast|southwest|"
    r"northerly|southerly|easterly|westerly)\s+of"
    r"|across\s+(?:from|the\s+street\s+from)"
    r"|adjacent\s+to"
    r"|behind"
    r"|nearby"
    r"|next\s+to"
    r"|\d[\d,]*\s*(?:feet|ft|foot|miles?|mi)\.?\s+"
    r"(?:north|south|east|west|northeast|northwest|southeast|southwest)\s+of"
    r")",
    flags=re.I,
)
INTERSECTION_RE = re.compile(
    r"\b(?P<a>[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,3}\s+(?:Road|Rd|Street|St|Avenue|Ave|Boulevard|Blvd|Drive|Dr|Lane|Ln|Parkway|Pkwy|Trail|Trl|Highway|Hwy|Way|Circle|Cir|Court|Ct|Terrace|Ter|Place|Pl))\s*(?:&|and|/|\bat\b)\s*(?P<b>[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,3}\s+(?:Road|Rd|Street|St|Avenue|Ave|Boulevard|Blvd|Drive|Dr|Lane|Ln|Parkway|Pkwy|Trail|Trl|Highway|Hwy|Way|Circle|Cir|Court|Ct|Terrace|Ter|Place|Pl))",
)
ACTION_PREFIX_WORDS = {
    "repave", "resurface", "resurfacing", "widen", "widening", "reconstruct",
    "improve", "improvements", "improvement", "study", "design",
    "complete", "completion", "rebuild", "rebuilding", "extend", "extension",
    "phase", "project", "milestone", "construction", "approval",
    "approve", "authorize", "authorization", "adopt", "discuss", "discussion",
    "consider", "consideration", "hold", "receive", "execute", "award", "fund",
    "funding", "accept", "acceptance", "agreement", "amendment", "amend",
    "resolution", "ordinance", "task", "change", "order", "review", "reviewing",
}

_STREET_TOKEN = r"(?:[A-Z][A-Za-z.'\-]+|U\.S\.|US|\d+)"
_STREET_NAME = (
    rf"{_STREET_TOKEN}(?:\s+{_STREET_TOKEN}){{0,4}}\s+"
    r"(?:Road|Rd|Street|St|Avenue|Ave|Boulevard|Blvd|Drive|Dr|Lane|Ln|"
    r"Parkway|Pkwy|Trail|Trl|Highway|Hwy|Way|Circle|Cir|Court|Ct|"
    r"Terrace|Ter|Place|Pl)"
)
# Endpoint phrase can be a full street name, a route designation ("US 41",
# "I-75"), or a known cross-street short form ("Williams", "Three Oaks").
_ENDPOINT_PHRASE = (
    r"(?:U\.S\.\s+\d+|US\s+\d+|I-\d+|Interstate\s+\d+|"
    rf"{_STREET_TOKEN}(?:\s+{_STREET_TOKEN}){{0,5}})"
)
CORRIDOR_RE = re.compile(
    rf"\b(?P<street>{_STREET_NAME})\s+from\s+(?P<a>{_ENDPOINT_PHRASE})\s+to\s+(?P<b>{_ENDPOINT_PHRASE})",
)
WHOLE_STREET_RE = re.compile(
    r"\b(?:along|on)\s+(?P<street>[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,4}\s+(?:Road|Rd|Street|St|Avenue|Ave|Boulevard|Blvd|Drive|Dr|Lane|Ln|Parkway|Pkwy|Trail|Trl|Highway|Hwy|Way|Circle|Cir|Court|Ct|Terrace|Ter|Place|Pl))\b",
)
ANCHORED_OFFSET_RE = re.compile(
    r"\b(?P<distance>\d[\d,]{1,6})\s*(?P<unit>feet|ft|foot)\s*(?P<direction>north|south|east|west|northeast|northwest|southeast|southwest)\s+of\s+(?P<anchor>[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,4}\s+(?:Road|Rd|Street|St|Avenue|Ave|Boulevard|Blvd|Drive|Dr|Lane|Ln|Parkway|Pkwy|Trail|Trl))",
    flags=re.I,
)


class LocationResolver:
    """Classify an agenda-item text reference into a typed location and resolve it."""

    def __init__(
        self,
        cache_dir: Path = Path(".cache/leepa"),
        venue_lookup: dict[str, dict] | None = None,
    ) -> None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        self.requester = _CachedRequester(cache_dir / "location_resolver.json")
        self.parcels = ParcelClient(self.requester)
        self.roads = RoadClient(self.requester)
        self.neighborhoods = NeighborhoodClient(self.requester)
        self.parks = ParkClient(self.requester)
        # venue_lookup is keyed by canonical name → {"latitude": .., "longitude": ..,
        # "address": ..., "aliases": [...]}.  When provided, named-venue lookups
        # consult it before hitting the parks layer.
        self.venue_lookup = venue_lookup or {}
        # Build an alias→canonical map for fast text matching.  Skip aliases
        # that look like generic street names — those should be handled by the
        # corridor / whole-street resolvers, not pinned to a single seed point.
        self._venue_alias_index: list[tuple[str, str]] = []
        for canonical, data in self.venue_lookup.items():
            aliases = {canonical, *(data.get("aliases") or [])}
            for alias in aliases:
                if not alias or _looks_like_generic_street(alias):
                    continue
                self._venue_alias_index.append((alias.lower(), canonical))
        # Longer aliases first so "Estero Sports Park" beats "Estero".
        self._venue_alias_index.sort(key=lambda x: -len(x[0]))

    def flush(self) -> None:
        self.requester.flush()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def resolve(self, text: str, *, item_title: str | None = None) -> LocationReference | None:
        """Return the best single LocationReference for an agenda item, or None.

        We try every applicable resolver and keep the highest-confidence hit.
        """
        text = (text or "").strip()
        if not text:
            return None
        # Search corpus is the title + body for richer pattern matching.
        full_text = f"{item_title or ''}\n{text}".strip()

        candidates: list[LocationReference] = []

        # Order matters: most specific / highest-confidence resolvers first.
        # A single parcel address is the most precise reference an agenda
        # item can carry — it should win over a venue or intersection that
        # the item merely mentions as context.
        for resolver in (
            self._try_single_parcel,
            self._try_multi_parcel,
            self._try_intersection,
            self._try_corridor,
            self._try_anchored_offset,
            self._try_park,
            self._try_named_venue,
            self._try_whole_street,
            self._try_neighborhood,
        ):
            try:
                ref = resolver(full_text)
            except RuntimeError:
                continue
            if ref:
                candidates.append(ref)
                # Confidence > 0.9 is good enough to short-circuit so we don't
                # waste cycles on lower-confidence resolvers.
                if ref.confidence >= 0.9:
                    break

        if not candidates:
            return None
        candidates.sort(key=lambda r: r.confidence, reverse=True)
        return candidates[0]

    def resolve_all(self, text: str, *, item_title: str | None = None) -> list[LocationReference]:
        """Return ALL distinct sites an agenda item references.

        Most items resolve to a single point — this just wraps resolve() in a
        list.  Items that genuinely reference multiple parcels (e.g. Via
        Coconut: "8990 Corkscrew Road, 21650 & 21750 Via Coconut Point,
        21331, 21350 & 21351 Happy Hollow Lane") fan out into one ref per
        parcel.

        Conservative by design — descriptive context phrases like:

          • "located south of Corkscrew Road"
          • "1,000 feet west of US 41"
          • "north of the Estero Health Center"
          • "across from Fountain Lakes"

        are NOT treated as additional sites.  Only addresses that appear
        OUTSIDE such a context window contribute extra refs.
        """
        text = (text or "").strip()
        if not text:
            return []
        full_text = f"{item_title or ''}\n{text}".strip()

        primary = self.resolve(text, item_title=item_title)
        if primary is None:
            return []
        refs: list[LocationReference] = [primary]

        # Fan-out only makes sense for parcel-anchored items.  For corridors,
        # whole-streets, named venues, etc. the "single representative point"
        # contract holds — the typical multi-address pattern doesn't apply.
        if primary.location_type not in {"PARCEL_ADDRESS", "MULTI_PARCEL"}:
            return refs

        # Seed dedup sets with whatever the primary already covered.
        seen_pairs: set[tuple[str, str]] = set()
        seen_straps: set[str] = set()
        if primary.parcel_strap:
            seen_straps.add(primary.parcel_strap)
        for s in primary.feature_ids or []:
            if s:
                seen_straps.add(s)
        seed_match_single = SINGLE_ADDRESS_RE.search(primary.raw_text)
        seed_match_multi = MULTI_ADDRESS_RE.search(primary.raw_text)
        if seed_match_multi:
            street = _normalize_pair_street(seed_match_multi.group("street"))
            seen_pairs.add((seed_match_multi.group("n1"), street))
            seen_pairs.add((seed_match_multi.group("n2"), street))
        elif seed_match_single:
            street = _normalize_pair_street(seed_match_single.group("street"))
            seen_pairs.add((seed_match_single.group("num"), street))

        for num, street_raw in self._scan_distinct_addresses(full_text):
            key = (num, _normalize_pair_street(street_raw))
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            new_ref = self._resolve_extra_parcel(num, street_raw, seen_straps=seen_straps)
            if new_ref is None:
                continue
            refs.append(new_ref)
            if new_ref.parcel_strap:
                seen_straps.add(new_ref.parcel_strap)
            for s in new_ref.feature_ids or []:
                if s:
                    seen_straps.add(s)

        return refs

    def _scan_distinct_addresses(self, text: str) -> list[tuple[str, str]]:
        """Yield every (number, street) pair that is NOT inside a descriptive
        context window. Compound forms (X, Y & Z Street) expand into one pair
        per number; single addresses contribute one pair each.

        Ordering preserves text order so callers writing fan-out rows get
        stable location_seq values across re-runs.
        """
        pairs: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()

        # Pass 1: compound (X, Y & Z Street) — these are unambiguous multi-parcel
        # references because the regex requires a street suffix.
        for m in COMPOUND_ADDRESS_RE.finditer(text):
            if _in_directional_context(text, m.start()):
                continue
            street_raw = trim_action_prefix(_trim_at_sentence_boundary(m.group("street")))
            if not _looks_like_street(street_raw):
                continue
            for grp in ("n1", "n2", "n3", "n4", "n5"):
                v = m.group(grp)
                if not v:
                    continue
                if _is_reference_number(text, m.start(grp)):
                    continue
                key = (v, _normalize_pair_street(street_raw))
                if key in seen:
                    continue
                seen.add(key)
                pairs.append((v, street_raw))

        # Pass 2: standalone single addresses elsewhere in the text.
        for m in SINGLE_ADDRESS_RE.finditer(text):
            if _in_directional_context(text, m.start()):
                continue
            if _is_reference_number(text, m.start("num")):
                continue
            street_raw = trim_action_prefix(_trim_at_sentence_boundary(m.group("street")))
            if not _looks_like_street(street_raw):
                continue
            num = m.group("num")
            key = (num, _normalize_pair_street(street_raw))
            if key in seen:
                continue
            seen.add(key)
            pairs.append((num, street_raw))

        return pairs

    def _resolve_extra_parcel(
        self,
        num: str,
        street_raw: str,
        *,
        seen_straps: set[str],
    ) -> LocationReference | None:
        """Resolve one (number, street) pair into a PARCEL_ADDRESS ref, or
        return None if the parcel layer has no match or every hit was already
        seen as part of an earlier ref.
        """
        for variant in street_search_variants(street_raw):
            hits = self.parcels.parcels_at_address(num, variant)
            if not hits:
                continue
            hit_straps = [h.get("STRAP", "") for h in hits if h.get("STRAP")]
            if hit_straps and all(s in seen_straps for s in hit_straps):
                return None
            centroids = [(h["_lon"], h["_lat"]) for h in hits if "_lon" in h]
            if not centroids:
                continue
            center = average_point(centroids)
            if center is None:
                continue
            strap = hits[0].get("STRAP", "")
            return LocationReference(
                location_type="PARCEL_ADDRESS",
                raw_text=f"{num} {street_raw}",
                latitude=center[1], longitude=center[0],
                confidence=0.95 if len(hits) == 1 else 0.90,
                address_label=hits[0].get("SITEADDR", ""),
                resolution_notes=f"parcel STRAP={strap} (additional site)",
                parcel_strap=strap,
                feature_ids=hit_straps,
            )
        return None

    # ------------------------------------------------------------------
    # Type-specific resolvers
    # ------------------------------------------------------------------

    def _try_single_parcel(self, text: str) -> LocationReference | None:
        for m in SINGLE_ADDRESS_RE.finditer(text):
            if _is_reference_number(text, m.start("num")):
                continue
            num = m.group("num")
            street_raw = trim_action_prefix(m.group("street").rstrip(".,;)"))
            if not _looks_like_street(street_raw):
                continue
            for variant in street_search_variants(street_raw):
                hits = self.parcels.parcels_at_address(num, variant)
                if hits:
                    centroids = [(h["_lon"], h["_lat"]) for h in hits if "_lon" in h]
                    if not centroids:
                        continue
                    center = average_point(centroids)
                    if center is None:
                        continue
                    strap = hits[0].get("STRAP", "")
                    return LocationReference(
                        location_type="PARCEL_ADDRESS",
                        raw_text=m.group(0),
                        latitude=center[1], longitude=center[0],
                        confidence=0.98 if len(hits) == 1 else 0.92,
                        address_label=hits[0].get("SITEADDR", ""),
                        resolution_notes=f"parcel STRAP={strap}",
                        parcel_strap=strap,
                        feature_ids=[h.get("STRAP", "") for h in hits],
                    )
        return None

    def _try_multi_parcel(self, text: str) -> LocationReference | None:
        m = MULTI_ADDRESS_RE.search(text)
        if not m:
            return None
        if _is_reference_number(text, m.start("n1")):
            return None
        n1, n2 = m.group("n1"), m.group("n2")
        street_raw = trim_action_prefix(m.group("street").rstrip(".,;)"))
        if not _looks_like_street(street_raw):
            return None
        all_centroids: list[tuple[float, float]] = []
        straps: list[str] = []
        addresses: list[str] = []
        for variant in street_search_variants(street_raw):
            for num in (n1, n2):
                for hit in self.parcels.parcels_at_address(num, variant):
                    if "_lon" in hit:
                        all_centroids.append((hit["_lon"], hit["_lat"]))
                        if hit.get("STRAP"):
                            straps.append(hit["STRAP"])
                        if hit.get("SITEADDR"):
                            addresses.append(hit["SITEADDR"])
            if all_centroids:
                break
        if not all_centroids:
            return None
        center = average_point(all_centroids)
        if center is None:
            return None
        # Use one canonical address as the label so the verifier and frontend
        # can parse it; full list of matched parcels lives in feature_ids.
        primary_address = addresses[0] if addresses else m.group(0)
        return LocationReference(
            location_type="MULTI_PARCEL",
            raw_text=m.group(0),
            latitude=center[1], longitude=center[0],
            confidence=0.93,
            address_label=primary_address[:200],
            resolution_notes=f"averaged centroid of {len(all_centroids)} parcels: {', '.join(dict.fromkeys(addresses))[:200]}",
            feature_ids=straps,
        )

    def _try_intersection(self, text: str) -> LocationReference | None:
        m = INTERSECTION_RE.search(text)
        if not m:
            return None
        street_a = trim_action_prefix(m.group("a"))
        street_b = trim_action_prefix(m.group("b"))
        variants_a = street_search_variants(street_a)
        core_b = normalize_street_core(street_b)
        if not variants_a or not core_b:
            return None
        segs_a = self.roads.segments_for_street_variants(variants_a)
        if not segs_a:
            return None
        # Find a segment along street A whose FROMSTREET or TOSTREET starts
        # with the same prefix as street B.  Use the endpoint that lies on B.
        for seg in segs_a:
            attrs = seg.get("attributes", {})
            fs = (attrs.get("FROMSTREET") or "").upper()
            ts = (attrs.get("TOSTREET") or "").upper()
            paths = seg.get("geometry", {}).get("paths", [])
            if not paths:
                continue
            first = paths[0][0]
            last = paths[-1][-1]
            if core_b in fs:
                return LocationReference(
                    location_type="INTERSECTION",
                    raw_text=m.group(0),
                    latitude=first[1], longitude=first[0],
                    confidence=0.93,
                    address_label=f"{attrs.get('STREET','')} & {fs}",
                    resolution_notes=f"endpoint of segment with FROMSTREET={fs}",
                )
            if core_b in ts:
                return LocationReference(
                    location_type="INTERSECTION",
                    raw_text=m.group(0),
                    latitude=last[1], longitude=last[0],
                    confidence=0.93,
                    address_label=f"{attrs.get('STREET','')} & {ts}",
                    resolution_notes=f"endpoint of segment with TOSTREET={ts}",
                )
        return None

    def _try_corridor(self, text: str) -> LocationReference | None:
        m = CORRIDOR_RE.search(text)
        if not m:
            return None
        street = trim_action_prefix(m.group("street"))
        a = trim_action_prefix(m.group("a"))
        b = trim_action_prefix(m.group("b"))
        variants_main = street_search_variants(street)
        variants_a = street_search_variants(a)
        variants_b = street_search_variants(b)
        if not variants_main:
            return None
        segs = self.roads.segments_for_street_variants(variants_main)
        if not segs:
            return None
        # Identify the contiguous run of segments that lies between endpoint A
        # and endpoint B. Match against all street-name variants (US 41 →
        # S TAMIAMI TRL alias, "Three Oaks" → "THREE OAKS PKWY", etc.).
        chosen_paths: list[list[list[float]]] = []
        endpoint_match = False
        endpoint_variants = [v for v in variants_a + variants_b if v]
        for seg in segs:
            attrs = seg.get("attributes", {})
            fs = (attrs.get("FROMSTREET") or "").upper()
            ts = (attrs.get("TOSTREET") or "").upper()
            if any(v in fs or v in ts for v in endpoint_variants):
                endpoint_match = True
                for path in seg.get("geometry", {}).get("paths", []):
                    chosen_paths.append(path)
        if not endpoint_match:
            # Couldn't find named endpoints in the road layer; fall back to
            # taking the midpoint of all segments for that street, which behaves
            # like WHOLE_STREET — still a valid centroid for the corridor.
            for seg in segs:
                for path in seg.get("geometry", {}).get("paths", []):
                    chosen_paths.append(path)
            confidence = 0.7
        else:
            confidence = 0.85
        if not chosen_paths:
            return None
        center = best_midpoint_across_paths(chosen_paths, prefer_core=True)
        if center is None:
            return None
        return LocationReference(
            location_type="CORRIDOR",
            raw_text=m.group(0),
            latitude=center[1], longitude=center[0],
            confidence=0.92 if endpoint_match else 0.75,
            address_label=f"{street} from {a} to {b}",
            resolution_notes=f"midpoint of {len(chosen_paths)} segment(s)",
        )

    def _try_whole_street(self, text: str) -> LocationReference | None:
        m = WHOLE_STREET_RE.search(text)
        if not m:
            return None
        street = trim_action_prefix(m.group("street"))
        variants = street_search_variants(street)
        if not variants:
            return None
        segs = self.roads.segments_for_street_variants(variants)
        if not segs:
            return None
        all_paths: list[list[list[float]]] = []
        for seg in segs:
            for path in seg.get("geometry", {}).get("paths", []):
                all_paths.append(path)
        if not all_paths:
            return None
        center = best_midpoint_across_paths(all_paths, prefer_core=True)
        if center is None:
            return None
        return LocationReference(
            location_type="WHOLE_STREET",
            raw_text=m.group(0),
            latitude=center[1], longitude=center[0],
            confidence=0.7,
            address_label=f"along {street}",
            resolution_notes=f"midpoint of {len(all_paths)} centerline segment(s)",
        )

    def _try_anchored_offset(self, text: str) -> LocationReference | None:
        m = ANCHORED_OFFSET_RE.search(text)
        if not m:
            return None
        # If the same text also names an explicit street address, the address
        # is the authoritative site — the offset phrase is just descriptive.
        # Let the legacy geocoder path handle the address instead of pinning
        # to a road midpoint that can be miles off.
        if SINGLE_ADDRESS_RE.search(text):
            return None
        distance = float(m.group("distance").replace(",", ""))
        if distance <= 0:
            return None
        unit = m.group("unit").lower()
        if unit in {"feet", "ft", "foot"}:
            distance_m = distance * 0.3048
        else:
            distance_m = distance  # already metric
        direction_word = m.group("direction").lower()
        bearing = DIRECTION_TO_BEARING.get(DIRECTIONAL_WORDS.get(direction_word, ""))
        if bearing is None:
            return None
        anchor_street = trim_action_prefix(m.group("anchor"))
        variants = street_search_variants(anchor_street)
        if not variants:
            return None
        segs = self.roads.segments_for_street_variants(variants)
        if not segs:
            return None
        all_paths = [p for seg in segs for p in seg.get("geometry", {}).get("paths", [])]
        midpoint = best_midpoint_across_paths(all_paths)
        if midpoint is None:
            return None
        target = offset_point(midpoint, bearing, distance_m)
        return LocationReference(
            location_type="ANCHORED_OFFSET",
            raw_text=m.group(0),
            latitude=target[1], longitude=target[0],
            confidence=0.7,
            address_label=f"{int(distance)} {unit} {direction_word} of {anchor_street}",
            resolution_notes=f"offset from midpoint of {anchor_street} by {distance_m:.0f}m @ {bearing}°",
        )

    def _try_named_venue(self, text: str) -> LocationReference | None:
        # Use the existing match_locations() (with ALIAS_NEGATIVE_CONTEXT
        # suppression for cases like "Bert" vs "Bert Harris lawsuits") to
        # decide which canonical seed names actually apply to this text.
        from .classifiers import match_locations  # local import to avoid cycle
        matches = match_locations(text)
        for canonical in matches:
            data = self.venue_lookup.get(canonical) or {}
            if data.get("location_type") in (None, "", "Road", "Corridor"):
                # Streets/corridors should resolve via the road resolvers, not
                # be pinned to a single named-venue point.
                continue
            if _looks_like_generic_street(canonical):
                continue
            lat = data.get("latitude")
            lon = data.get("longitude")
            if lat in (None, "") or lon in (None, ""):
                continue
            return LocationReference(
                location_type="NAMED_VENUE",
                raw_text=canonical,
                latitude=float(lat), longitude=float(lon),
                confidence=0.9,
                address_label=str(data.get("address") or canonical),
                resolution_notes=f"named venue '{canonical}' from LOCATION_SEEDS",
            )
        return None

    def _try_neighborhood(self, text: str) -> LocationReference | None:
        # Use a small whitelist of recognisable subdivision name fragments to
        # avoid spurious matches on common nouns. Fragments that are ROAD
        # names in Estero ("Three Oaks", "Sandy Lane") must stay out of this
        # list: LIKE-matching them against the county neighborhoods layer
        # pins items to unrelated subdivisions miles away (e.g. "Three Oaks
        # Marketplace" in San Carlos Park).
        candidates = re.findall(
            r"\b(Pelican Sound|Pelican Landing|Estero Bay Village|Bella Terra|"
            r"Copperleaf|Grandezza|Spring Run|Mayfair Village|Vintage Trace|"
            r"Highland Woods|Stoneybrook|Pelican Preserve|Wildcat Run|"
            r"Wildblue|Verandah|Marsh Landing|Tidewater|Brightwork|Tides at Pelican|"
            r"Breckenridge|Riverwoods Plantation|Corkscrew Woodlands|Cascades|"
            r"Rookery Pointe|River Oaks)\b",
            text,
            flags=re.I,
        )
        for cand in candidates:
            core = cand.upper()
            hits = self.neighborhoods.neighborhoods_by_name(core)
            if hits:
                best = hits[0]
                if "_lon" not in best:
                    continue
                return LocationReference(
                    location_type="NEIGHBORHOOD",
                    raw_text=cand,
                    latitude=best["_lat"], longitude=best["_lon"],
                    confidence=0.85,
                    address_label=str(best.get("descriptive_name") or best.get("name") or cand),
                    resolution_notes=f"centroid of community '{best.get('name')}'",
                )
        return None

    def _try_park(self, text: str) -> LocationReference | None:
        # Lee County's Parks layer covers public parks and recreation facilities.
        # Match a capitalized phrase ending in Park/Preserve/Center,
        # optionally prefixed by Sports/State/etc.
        candidates: list[str] = []
        for match in re.finditer(
            r"\b([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,4}\s+(?:Sports\s+Park|State\s+Park|Park|Preserve|Recreation\s+Center|Community\s+Center))\b",
            text,
        ):
            cand = match.group(1)
            if cand.lower().startswith(("the ", "village ", "approve ", "ordinance ")):
                continue
            candidates.append(cand)
        for cand in candidates:
            # Strip trailing place-type word(s) to get the search core.
            core = re.sub(
                r"\s+(?:Sports\s+Park|State\s+Park|Park|Preserve|Recreation\s+Center|Community\s+Center)$",
                "",
                cand,
                flags=re.I,
            ).strip().upper()
            if len(core) < 3:
                continue
            hits = self.parks.parks_by_name(core)
            if hits:
                best = hits[0]
                if "_lon" not in best:
                    continue
                return LocationReference(
                    location_type="NAMED_VENUE",
                    raw_text=cand,
                    latitude=best["_lat"], longitude=best["_lon"],
                    confidence=0.88,
                    address_label=str(best.get("Name") or cand),
                    resolution_notes=f"park location '{best.get('Name')}'",
                )
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STREET_SUFFIX_PATTERN = re.compile(
    r"(?:Road|Rd|Street|St|Avenue|Ave|Boulevard|Blvd|Drive|Dr|Lane|Ln|"
    r"Parkway|Pkwy|Trail|Trl|Highway|Hwy|Way|Circle|Cir|Court|Ct|"
    r"Terrace|Ter|Place|Pl|Point|Pt)",
    re.I,
)
_NON_STREET_WORDS = {
    "contract", "engineering", "services", "construction", "budget",
    "workshop", "minutes", "agenda", "meeting", "action", "amendment",
    "acceptance", "provide", "proposed", "replace", "repair",
    "resolution", "ordinance", "fiscal", "year",
}


def _looks_like_street(text: str) -> bool:
    if not _STREET_SUFFIX_PATTERN.search(text):
        return False
    tokens = {t.lower() for t in text.split()}
    return not (tokens & _NON_STREET_WORDS)


def _trim_at_sentence_boundary(text: str) -> str:
    """Strip everything after the first sentence-terminating period.

    SINGLE_ADDRESS_RE's street character class allows '.', which means
    "21351 Happy Hollow Lane. Properties are located south" gets captured
    as a street of "Happy Hollow Lane. Properties".  Trimming at the first
    period yields the actual street name.
    """
    if "." in text:
        text = text.split(".", 1)[0]
    return text.rstrip(".,;)").strip()


def _in_directional_context(text: str, position: int, window: int = 80) -> bool:
    """True if a descriptive-context phrase ends within ``window`` chars before
    ``position``.  Used by resolve_all to skip addresses that are merely being
    used to describe where a single site sits (e.g. "north of 12345 Main St").
    """
    start = max(0, position - window)
    snippet = text[start:position]
    if not snippet:
        return False
    last = None
    for m in DIRECTIONAL_CONTEXT_RE.finditer(snippet):
        last = m
    if last is None:
        return False
    # Require the context phrase to actually butt up against the address —
    # anything after the phrase should be at most a few words (the address
    # itself plus an optional "the" / article).  Long passages between the
    # phrase and the address mean it's a separate clause, not a qualifier.
    tail = snippet[last.end():]
    return len(tail.strip()) <= 30


def _normalize_pair_street(text: str) -> str:
    """Canonicalize a street name for use as a dedup key.  Drops trailing
    punctuation, collapses whitespace, uppercases — does NOT strip suffix.
    Two refs to the "same" street should produce the same key here even if
    one wrote "Corkscrew Rd" and the other "Corkscrew Road"; for that we
    normalize via parse_street_parts when available.
    """
    parts = parse_street_parts(text)
    if parts:
        base, suffix, direction = parts
        bits = [base]
        if suffix:
            bits.append(suffix)
        if direction:
            bits.append(direction)
        return " ".join(bits)
    return re.sub(r"\s+", " ", text.strip().upper().rstrip(".,;)"))


def _looks_like_generic_street(text: str) -> bool:
    """True if the text is a bare street name or a corridor description.

    These should be resolved as CORRIDOR or WHOLE_STREET, not as a single
    named-venue point — even if a LOCATION_SEEDS entry happens to alias them.
    """
    if re.search(r"\bfrom\b.*\bto\b", text, flags=re.I):
        return True
    # Strip a trailing city/state so "Estero Parkway, Estero, FL" reads as a street.
    head = re.split(r",", text)[0].strip()
    tokens = head.split()
    if not tokens:
        return False
    last = tokens[-1].lower().rstrip(".")
    return last in STREET_SUFFIX_SYNONYMS


def text_signature(text: str) -> str:
    """Stable hash of input text — handy as a cache key for resolve() calls."""
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:12]
