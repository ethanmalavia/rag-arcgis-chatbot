"""Cross-check each agenda map coordinate against the Lee County parcel layer.

For every row in arcgis_agenda_map_data.csv we ask the Lee County Property
Appraiser FeatureServer two questions:

1. Which parcel contains the (lat, lon) we placed on the map?
2. Which parcel matches the street address text we extracted from the PDF?

If both answers agree on a parcel STRAP, the row is VERIFIED. If they disagree,
the row is MISMATCH (or ADJACENT if the parcels are within ~80 m of each
other). If the point isn't in any parcel, or the address can't be found in the
parcel layer, those become separate triage buckets.

Output: normalized_csv/review/location_verification.csv (one row per input row).
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


PARCEL_QUERY_URL = (
    "https://services2.arcgis.com/LvWGAAhHwbCJ2GMP/arcgis/rest/services/"
    "Lee_County_Parcels/FeatureServer/0/query"
)
PARCEL_OUT_FIELDS = "STRAP,SITEADDR,SITENUMBER,SITESTREET,SITECITY,SITEZIP"

ADJACENT_THRESHOLD_M = 80.0
HTTP_TIMEOUT_S = 30
PAUSE_BETWEEN_CALLS_S = 0.05

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE


# Street suffix synonyms used to normalise both directions
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
}

DIRECTIONAL_TOKENS = {"N", "S", "E", "W", "NE", "NW", "SE", "SW"}
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


def _parse_single_address(cleaned: str) -> tuple[str, str] | None:
    m = re.match(r"^(\d{1,6})\s+(.+)$", cleaned)
    if not m:
        return None
    number = m.group(1)
    tokens = m.group(2).split()
    # Strip trailing periods on individual tokens ("So." → "So", "S." → "S")
    # so directional/suffix lookups match.
    tokens = [t.rstrip(".") for t in tokens if t.rstrip(".")]
    if not tokens:
        return None

    # Strip trailing directional (e.g. "Broadway Avenue East" → "Broadway Avenue", dir=E).
    # Capture it so we can re-attach to the search core, since the parcel layer
    # encodes "BROADWAY E" not "BROADWAY EAST".
    trailing_dir = ""
    if len(tokens) >= 2 and tokens[-1].lower() in DIRECTIONAL_WORDS:
        trailing_dir = DIRECTIONAL_WORDS[tokens[-1].lower()]
        tokens = tokens[:-1]
    # Then strip the street suffix word, if any.
    if tokens and tokens[-1].lower() in STREET_SUFFIX_SYNONYMS:
        tokens = tokens[:-1]
    # And a trailing directional that sat *between* name and suffix
    # (rare, but covers "Broadway East Avenue").
    if not trailing_dir and len(tokens) >= 2 and tokens[-1].lower() in DIRECTIONAL_WORDS:
        trailing_dir = DIRECTIONAL_WORDS[tokens[-1].lower()]
        tokens = tokens[:-1]
    # Normalise a leading directional word so "South Tamiami" matches "S TAMIAMI".
    if tokens and tokens[0].lower() in DIRECTIONAL_WORDS:
        tokens[0] = DIRECTIONAL_WORDS[tokens[0].lower()]
    if not tokens:
        return None
    core = " ".join(t.upper() for t in tokens)
    core = re.sub(r"[^A-Z0-9 ]", "", core).strip()
    if not core:
        return None
    if trailing_dir:
        core = f"{core} {trailing_dir}"
    return number, core


def parse_address(text: str) -> list[tuple[str, str]]:
    """Parse a location string into one or more (number, street_core) tuples.

    Returns a list because some PDFs reference multiple parcels in one item
    (e.g. "20741 and 20771 S. Tamiami Trail" or "10170 and 10150 Arcos Avenue").
    Each tuple is suitable for a `SITENUMBER='N' AND SITESTREET LIKE '%core%'`
    query on the Lee County parcel layer.
    """
    if not text:
        return []
    cleaned = re.sub(
        r",\s*(?:estero|fort myers|bonita springs|fl|florida)[\s\d,]*$",
        "",
        text,
        flags=re.I,
    )
    cleaned = re.split(r",", cleaned)[0].strip()
    if not cleaned:
        return []

    # Detect "<num> and <num> <street>" or "<num> & <num> <street>" early
    # so we can produce two parse results that share the same street body.
    multi = re.match(
        r"^(\d{1,6})\s+(?:and|&)\s+(\d{1,6})\s+(.+)$",
        cleaned,
        flags=re.I,
    )
    if multi:
        n1, n2, tail = multi.group(1), multi.group(2), multi.group(3)
        parses = []
        for n in (n1, n2):
            p = _parse_single_address(f"{n} {tail}")
            if p:
                parses.append(p)
        # Deduplicate while preserving order
        seen: set[tuple[str, str]] = set()
        unique: list[tuple[str, str]] = []
        for p in parses:
            if p not in seen:
                seen.add(p)
                unique.append(p)
        return unique

    p = _parse_single_address(cleaned)
    return [p] if p else []


class ParcelClient:
    def __init__(self, cache_dir: Path) -> None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        self.point_cache_path = cache_dir / "leepa_point_query.json"
        self.address_cache_path = cache_dir / "leepa_address_query.json"
        self.point_cache: dict[str, dict] = self._load(self.point_cache_path)
        self.address_cache: dict[str, dict] = self._load(self.address_cache_path)
        self.calls = 0
        self.cache_hits = 0

    @staticmethod
    def _load(path: Path) -> dict:
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return {}
        return {}

    def _save(self) -> None:
        self.point_cache_path.write_text(
            json.dumps(self.point_cache, indent=2, sort_keys=True), encoding="utf-8"
        )
        self.address_cache_path.write_text(
            json.dumps(self.address_cache, indent=2, sort_keys=True), encoding="utf-8"
        )

    def _get(self, params: dict[str, str]) -> dict:
        url = PARCEL_QUERY_URL + "?" + urllib.parse.urlencode(params)
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(url, context=SSL_CTX, timeout=HTTP_TIMEOUT_S) as r:
                    return json.load(r)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
                last_err = e
                time.sleep(0.5 * (attempt + 1))
        raise RuntimeError(f"parcel query failed after retries: {last_err}")

    def parcel_at_point(self, lon: float, lat: float) -> list[dict]:
        key = f"{lon:.6f},{lat:.6f}"
        if key in self.point_cache:
            self.cache_hits += 1
            return self.point_cache[key]
        params = {
            "geometry": f"{lon},{lat}",
            "geometryType": "esriGeometryPoint",
            "inSR": "4326", "outSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": PARCEL_OUT_FIELDS,
            "returnGeometry": "false",
            "f": "json",
        }
        data = self._get(params)
        attrs = [ft.get("attributes", {}) for ft in data.get("features", [])]
        self.point_cache[key] = attrs
        self.calls += 1
        time.sleep(PAUSE_BETWEEN_CALLS_S)
        return attrs

    def parcels_at_address(self, number: str, street_core: str) -> list[dict]:
        key = f"{number}|{street_core}"
        if key in self.address_cache:
            self.cache_hits += 1
            return self.address_cache[key]
        where = f"SITENUMBER='{number}' AND UPPER(SITESTREET) LIKE '%{street_core}%'"
        params = {
            "where": where,
            "outFields": PARCEL_OUT_FIELDS,
            "returnGeometry": "false",
            "returnCentroid": "true",
            "outSR": "4326",
            "f": "json",
        }
        data = self._get(params)
        features = []
        for ft in data.get("features", []):
            attrs = dict(ft.get("attributes", {}))
            c = ft.get("centroid") or {}
            if c.get("x") is not None and c.get("y") is not None:
                attrs["_centroid_lon"] = c["x"]
                attrs["_centroid_lat"] = c["y"]
            features.append(attrs)
        self.address_cache[key] = features
        self.calls += 1
        time.sleep(PAUSE_BETWEEN_CALLS_S)
        return features

    def close(self) -> None:
        self._save()


def classify(
    point_parcels: list[dict],
    address_parcels: list[dict],
    point_lon: float,
    point_lat: float,
    addr_parsed: list[tuple[str, str]],
) -> tuple[str, float | None, str]:
    """Return (status, distance_m, note)."""
    if not addr_parsed:
        if point_parcels:
            return "VENUE_ONLY", None, f"point in {point_parcels[0].get('SITEADDR') or point_parcels[0].get('STRAP')}"
        return "VENUE_NO_PARCEL", None, "point not in any parcel and no parseable address"

    if not point_parcels and not address_parcels:
        return "BOTH_MISSING", None, "neither point nor address found in parcels"
    if not point_parcels:
        addr_one = address_parcels[0]
        return (
            "POINT_OUTSIDE_PARCEL",
            None,
            f"address resolves to {addr_one.get('SITEADDR')} ({addr_one.get('STRAP')}) but our point is in no parcel",
        )
    if not address_parcels:
        pt_one = point_parcels[0]
        return (
            "ADDRESS_NOT_FOUND",
            None,
            f"point is in {pt_one.get('SITEADDR')} ({pt_one.get('STRAP')}) but address text not in parcel layer",
        )

    point_straps = {p.get("STRAP") for p in point_parcels if p.get("STRAP")}
    addr_straps = {p.get("STRAP") for p in address_parcels if p.get("STRAP")}

    if point_straps & addr_straps:
        if len(address_parcels) > 1:
            return "VERIFIED_AMBIGUOUS_ADDR", 0.0, f"{len(address_parcels)} parcels share the address; point sits in one of them"
        return "VERIFIED", 0.0, ""

    # Different parcels. Compute distance from our point to the closest address-match centroid.
    distances = []
    for ap in address_parcels:
        c_lon = ap.get("_centroid_lon")
        c_lat = ap.get("_centroid_lat")
        if c_lon is None or c_lat is None:
            continue
        distances.append((haversine_m((point_lon, point_lat), (c_lon, c_lat)), ap))
    if not distances:
        return "MISMATCH", None, f"point in {point_parcels[0].get('SITEADDR')}, address resolves to {address_parcels[0].get('SITEADDR')} (no centroid for distance)"

    distances.sort(key=lambda x: x[0])
    closest_d, closest_addr = distances[0]
    note = (
        f"point in {point_parcels[0].get('SITEADDR')} ({point_parcels[0].get('STRAP')}); "
        f"address resolves to {closest_addr.get('SITEADDR')} ({closest_addr.get('STRAP')}) "
        f"~{closest_d:.0f}m away"
    )
    if closest_d <= ADJACENT_THRESHOLD_M:
        return "ADJACENT", closest_d, note
    if len(address_parcels) > 1:
        return "ADDRESS_AMBIGUOUS", closest_d, note + f" — {len(address_parcels)} address matches"
    return "MISMATCH", closest_d, note


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input",
        default="normalized_csv/arcgis/arcgis_agenda_map_data.csv",
        type=Path,
    )
    parser.add_argument(
        "--output",
        default="normalized_csv/review/location_verification.csv",
        type=Path,
    )
    parser.add_argument("--cache-dir", default=Path(".cache/leepa"), type=Path)
    parser.add_argument("--limit", type=int, default=0, help="Verify only the first N rows (0 = all)")
    parser.add_argument("--no-network", action="store_true", help="Use only cached responses; skip rows that would require a fresh call")
    args = parser.parse_args(argv)

    if not args.input.exists():
        print(f"Input not found: {args.input}", file=sys.stderr)
        return 1

    client = ParcelClient(args.cache_dir)

    with args.input.open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if args.limit:
        rows = rows[: args.limit]

    counts: dict[str, int] = {}
    out_rows: list[dict] = []
    skipped_offline = 0

    for i, row in enumerate(rows, 1):
        lat_s, lon_s = row.get("Latitude"), row.get("Longitude")
        if not lat_s or not lon_s:
            continue
        try:
            lat = float(lat_s)
            lon = float(lon_s)
        except ValueError:
            continue

        location_text = row.get("Location") or row.get("LocationName") or ""
        addr_parses = parse_address(location_text)

        try:
            if args.no_network:
                pt_key = f"{lon:.6f},{lat:.6f}"
                if pt_key not in client.point_cache:
                    skipped_offline += 1
                    continue
                point_parcels = client.point_cache[pt_key]
                address_parcels = []
                for n, core in addr_parses:
                    addr_key = f"{n}|{core}"
                    if addr_key not in client.address_cache:
                        skipped_offline += 1
                        address_parcels = None
                        break
                    address_parcels.extend(client.address_cache[addr_key])
                if address_parcels is None:
                    continue
            else:
                point_parcels = client.parcel_at_point(lon, lat)
                address_parcels = []
                for n, core in addr_parses:
                    address_parcels.extend(client.parcels_at_address(n, core))
        except RuntimeError as e:
            status, distance, note = "QUERY_ERROR", None, str(e)
            point_parcels, address_parcels = [], []
        else:
            status, distance, note = classify(point_parcels, address_parcels, lon, lat, addr_parses)

        counts[status] = counts.get(status, 0) + 1
        out_rows.append({
            "AgendaItemID": row.get("AgendaItemID"),
            "MeetingDate": row.get("MeetingDate"),
            "ProjectTitle": row.get("ProjectTitle"),
            "Location": location_text,
            "Latitude": lat_s,
            "Longitude": lon_s,
            "ParsedNumber": " / ".join(p[0] for p in addr_parses),
            "ParsedStreet": " / ".join(p[1] for p in addr_parses),
            "PointParcelSTRAP": (point_parcels[0].get("STRAP") if point_parcels else ""),
            "PointParcelAddress": (point_parcels[0].get("SITEADDR") if point_parcels else ""),
            "AddressParcelSTRAP": (address_parcels[0].get("STRAP") if address_parcels else ""),
            "AddressParcelAddress": (address_parcels[0].get("SITEADDR") if address_parcels else ""),
            "AddressMatchCount": len(address_parcels),
            "DistanceMeters": f"{distance:.0f}" if isinstance(distance, (int, float)) else "",
            "Status": status,
            "Notes": note,
            "Document_Link": row.get("Document_Link"),
        })

        if i % 50 == 0:
            client._save()
            print(f"  {i}/{len(rows)} processed (calls={client.calls}, cache={client.cache_hits})", flush=True)

    client.close()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "AgendaItemID", "MeetingDate", "ProjectTitle", "Location",
        "Latitude", "Longitude", "ParsedNumber", "ParsedStreet",
        "PointParcelSTRAP", "PointParcelAddress",
        "AddressParcelSTRAP", "AddressParcelAddress",
        "AddressMatchCount", "DistanceMeters", "Status", "Notes",
        "Document_Link",
    ]
    with args.output.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(out_rows)

    print()
    print(f"Wrote {len(out_rows)} verification rows to {args.output}")
    print(f"Network calls: {client.calls}; cache hits: {client.cache_hits}; skipped (offline): {skipped_offline}")
    print("Status breakdown:")
    triage_order = [
        "MISMATCH", "POINT_OUTSIDE_PARCEL", "ADDRESS_NOT_FOUND",
        "ADDRESS_AMBIGUOUS", "BOTH_MISSING", "VENUE_NO_PARCEL",
        "ADJACENT", "VERIFIED_AMBIGUOUS_ADDR", "VERIFIED", "VENUE_ONLY",
        "QUERY_ERROR",
    ]
    for status in triage_order:
        if status in counts:
            print(f"  {counts[status]:5}  {status}")
    for status, count in counts.items():
        if status not in triage_order:
            print(f"  {count:5}  {status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
