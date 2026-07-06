# Pipeline internals

This directory contains the EagleGIS data pipeline, which extracts Estero
municipal meeting minutes (Village Council + Planning Zoning & Design Board
PDFs in `pdfs/`) into the normalized CSVs under `data/`. What
follows is the operational reference: CLI flags, internal modules, the
meeting-type model, OCR fallback, the location resolver internals,
deliverable schemas, and the review workflow.

---

## Entry points

```powershell
# Standard run (most common)
python pipeline/build.py `
    --pdf-dir pdfs `
    --source-csv pdfs/Estero_Meetings_Final.csv `
    --out-dir data

# Verifier
python pipeline/verify.py
```

### `pipeline/build.py` flags

| Flag | Default | What it does |
|---|---|---|
| `--pdf-dir` | `pdfs` | Local directory holding meeting PDFs |
| `--git-ref` | none | Read PDFs from a git ref (e.g. `origin/script`) instead of `--pdf-dir` |
| `--source-csv` | none | Legacy `Estero_Meetings_Final.csv` path |
| `--source-git-ref` | `origin/script` | Git ref the legacy CSV lives on if `--source-csv` is absent |
| `--source-git-path` | `pdfs/Estero_Meetings_Final.csv` | Path to the legacy CSV inside that ref |
| `--out-dir` | `data` | Where to write all output CSVs |
| `--max-pages` | none | Cap pages per PDF for fast debugging |

If `--pdf-dir` doesn't exist locally, the loader falls back to reading
PDFs from `origin/script` automatically.

### `pipeline/verify.py` flags

| Flag | Default | What it does |
|---|---|---|
| `--input` | `data/gold/arcgis/arcgis_agenda_map_data.csv` | Input map data to verify |
| `--output` | `data/silver/review/location_verification.csv` | Where to write the triage report |
| `--cache-dir` | `.cache/leepa` | Where to persist Lee County API responses between runs |
| `--limit` | `0` (all) | Verify only the first N rows (useful for smoke tests) |
| `--no-network` | off | Skip rows whose API responses aren't already cached |

---

## Module layout

```
pipeline/
├── build.py                 main orchestrator
├── verify.py                Lee County parcel cross-check
└── eaglegis/                internal package
    ├── sources.py           PDF discovery (local dir or git ref)
    ├── text.py              PyMuPDF text extraction + OCR fallback
    ├── extractors.py        agenda-item parser, meeting metadata,
    │                        section detection, vote/motion text
    ├── classifiers.py       action_type + category inference,
    │                        project/location alias matching, address regex
    ├── location_resolver.py typed location resolution
    │                        (see "Location resolver" below)
    ├── config.py            category taxonomy, project aliases, location
    │                        seeds, site overrides, geocode hints
    └── writer.py            CSV writer helpers
```

`build.py` orchestrates the whole run:

1. **Load source CSV** (`Estero_Meetings_Final.csv`) — legacy ArcGIS metadata
2. **Load meeting PDFs** from `--pdf-dir` or `--git-ref`
3. **Extract text** per PDF via `text.py` (with OCR fallback)
4. **Infer meeting metadata** via `extractors.py` — date, board, format,
   venue, staff code, status
5. **Split into agenda entries** + action / motion records
6. **Classify** each item — action type, category, projects, locations
7. **Resolve locations** via `location_resolver.py` — produces one typed
   point per agenda item
8. **Apply geocode cache** from `data/bronze/geocoded_locations.csv`
   for any item the resolver couldn't pin
9. **Write CSVs** — see ["Pipeline deliverables"](#pipeline-deliverables) below

---

## OCR fallback

The first pass uses PyMuPDF text extraction. If a PDF has too little
embedded text (scanned originals), `text.py` falls back to Tesseract OCR
through `pytesseract`.

On Windows the loader auto-detects Tesseract at:

- `C:\Program Files\Tesseract-OCR\tesseract.exe`
- `C:\Program Files (x86)\Tesseract-OCR\tesseract.exe`
- any `tesseract` already on `PATH`

If Tesseract is missing, the PDF is still processed with whatever embedded
text exists, and the file is flagged in `extraction_review.csv`.

---

## Meeting-type model

`extractors.py` produces two levels:

- **Database-facing grouped type** (4 values, used in the relational
  model): `Village Council`, `Planning Zoning & Design Board`,
  `Public Hearing`, `Workshop`
- **Detailed format label** (preserved in `meeting_formats.csv`):
  `Regular Meeting`, `Special Meeting`, `Workshop`, `Zoning Hearing`,
  `Budget Hearing`, `Organizational Meeting`, `Cancelled`, …

Cancellation detection is strict: a meeting is marked `Cancelled` only when
the filename contains `cancel` *or* the first 600 chars match an explicit
cancellation-notice phrase (`notice of cancellation`, `this meeting has
been cancelled`, `cancelled meeting notice`, etc.). Body-text mentions of a
separately-cancelled item don't trigger it.

---

## Classifier internals

`classifiers.py` does three things:

1. **`infer_action_type(text, meeting_type)`** — categorises an agenda
   item's action: `Ordinance`, `Resolution`, `Contract Approval`,
   `Consent Agenda`, `Public Comment`, `Discussion`, `Vote`, etc.
2. **`infer_category(text, action_type)`** — assigns to one of the eight
   public-facing categories using a weighted term-match against
   `CATEGORY_TERMS` in `config.py`. **Subject categories beat support
   categories whenever both score**, so a "Contract for road widening"
   routes to *Transportation* rather than *Budget*.
3. **`match_projects` / `match_locations`** — looks up known
   project / location aliases with `ALIAS_NEGATIVE_CONTEXT` suppression
   (e.g. the alias `"bert"` is suppressed when the surrounding text is
   actually about a "Bert Harris" lawsuit, not the Bert Trail).

---

## Location resolver

`location_resolver.py` is the typed location pipeline. It classifies each
agenda-item text into one of nine reference types and resolves it to a
single best point. Resolvers run in confidence order; the first match
that returns ≥0.90 confidence short-circuits the rest.

| Order | Resolver | Confidence | Source |
|---|---|---:|---|
| 1 | `_try_single_parcel`    | 0.98 | Lee County parcel layer (`SITENUMBER` + `SITESTREET`) |
| 2 | `_try_multi_parcel`     | 0.93 | "X and Y Street" — averaged centroid |
| 3 | `_try_intersection`     | 0.93 | Road centerline segment endpoint |
| 4 | `_try_corridor`         | 0.92 / 0.75 | Road segments between named endpoints |
| 5 | `_try_anchored_offset`  | 0.70 | Anchor street + bearing + distance |
| 6 | `_try_park`             | 0.88 | Lee County Parks layer |
| 7 | `_try_named_venue`      | 0.90 | `LOCATION_SEEDS` + `match_locations()` |
| 8 | `_try_whole_street`     | 0.70 | Length-weighted centroid of all in-Estero segments |
| 9 | `_try_neighborhood`     | 0.85 | Lee County Neighborhoods layer |

The resolver caches every external API response to
`.cache/leepa/location_resolver.json`. After the first warmed run, repeats
make zero network calls.

Reference data sources (all Lee County public ArcGIS REST endpoints):

```
Lee_County_Parcels/FeatureServer/0
RoadCenterline/FeatureServer/0
Neighborhoods_and_Areas/FeatureServer/0
Park_Locations/FeatureServer/0
```

### Why "always a single point"

By design every agenda item resolves to **one** (lat, lon) so the frontend
stays simple. Corridor and whole-street references that geometrically want
a polyline are collapsed to the length-weighted centroid of their in-Estero
segments — a representative "middle of the road" pin. Named subdivisions
likewise collapse to a single community-polygon centroid.

This trades polyline fidelity for schema simplicity. If the project ever
needs proper line / polygon rendering, the data is there — `locations_v2.csv`
carries the `location_type` column so the frontend can branch on it.

### Manual overrides

Two override mechanisms in `config.py` short-circuit the resolver entirely:

- `SITE_LOCATION_OVERRIDES` — keyed by `application_id`
- `SITE_TEXT_LOCATION_OVERRIDES` — keyed by a substring of the item text

These are hand-curated and trusted absolutely — useful for items where the
resolver picks a wrong-but-plausible parcel and you want to pin the
correct one without retraining the classifier.

---

## Verifier output

`pipeline/verify.py` cross-checks every coordinate in
`arcgis_agenda_map_data.csv` against the Lee County Property Appraiser
parcel layer. Each row gets a `Status` for triage:

| Status | Meaning |
|---|---|
| `VERIFIED` | Our point and the address-derived parcel agree |
| `VERIFIED_AMBIGUOUS_ADDR` | Same parcel, address matched multiple units (condo / shared building) |
| `ADJACENT` | Different parcels but <80 m apart — boundary noise |
| `MISMATCH` | Different parcels >80 m apart — needs review |
| `POINT_OUTSIDE_PARCEL` | Address resolves cleanly but our point sits in no parcel |
| `ADDRESS_NOT_FOUND` | Our point is in a parcel, but the address text isn't in the Lee County roll |
| `BOTH_MISSING` | Neither point nor address resolves to a parcel |
| `VENUE_ONLY` | No parseable street number — point is in some parcel, can't cross-check |
| `VENUE_NO_PARCEL` | No parseable street number — point is in a road / water |

The verifier writes one CSV row per map point with columns:

`AgendaItemID`, `MeetingDate`, `ProjectTitle`, `Location`, `Latitude`,
`Longitude`, `ParsedNumber`, `ParsedStreet`, `PointParcelSTRAP`,
`PointParcelAddress`, `AddressParcelSTRAP`, `AddressParcelAddress`,
`AddressMatchCount`, `DistanceMeters`, `Status`, `Notes`, `Document_Link`.

Triage in order of priority: **MISMATCH → POINT_OUTSIDE_PARCEL →
ADDRESS_NOT_FOUND → ADDRESS_AMBIGUOUS → BOTH_MISSING → ADJACENT →
VERIFIED_AMBIGUOUS_ADDR → VERIFIED**. The `VENUE_*` buckets aren't
errors — they're items whose Location text has no parseable street number,
so the parcel cross-check doesn't apply.

---

## Review workflow

1. Run the pipeline: `python pipeline/build.py ...`
2. Confirm `data/gold/arcgis/arcgis_missing_coordinates.csv` has no data rows
3. Run the verifier: `python pipeline/verify.py`
4. Open `data/silver/review/location_verification.csv` in Excel / a viewer:
   - Filter `Status` to `MISMATCH` / `POINT_OUTSIDE_PARCEL` first — these
     are real placement errors and need either a coordinate fix in
     `review/geocoded_locations.csv` or a manual override in `config.py`
   - Then `ADDRESS_NOT_FOUND` / `BOTH_MISSING` — addresses that don't
     exist in the Lee County roll; usually a typo or a new lot
   - `VENUE_ONLY` / `VENUE_NO_PARCEL` rows aren't errors, but spot-check a
     few to make sure the resolver picked the right venue
5. Open `data/silver/review/extraction_review.csv` for items the extractor wasn't confident on
6. Apply fixes (override in `config.py`, geocode in `review/geocoded_locations.csv`)
   and re-run

---

## Testing

```powershell
python -m pytest pipeline/tests -q
```

78 tests cover: address candidate extraction, agenda-entry parsing,
section detection, action / category inference, location alias matching
with negative-context suppression, cancellation detection,
multi-parcel address splitting, intersection / corridor / whole-street
patterns, meeting-date extraction, and agenda-entry dedupe.

Run a single test:

```powershell
python -m pytest pipeline/tests/test_pipeline_parsers.py::PipelineParserTests::test_category_industry_rock_mining
```

---

## Pipeline deliverables

`pipeline/build.py` writes everything under `data/`, grouped into
medallion tiers (matching the legacy EagleGIS repo and the chatbot's
`sync-data.yml` consumer contract):

- `bronze/` — hand-curated inputs the build reads but never regenerates
- `silver/` — validated relational tables (`core/`, `v2/`) and QA triage (`review/`)
- `gold/` — publication-ready deliverables: `meetings_ai_public.csv` (the
  chatbot corpus) and `arcgis/` (map exports + per-category layers)

### `silver/core/` — relational schema

| File | What it holds |
|---|---|
| `meetings.csv` | One row per meeting (date, type, venue, status, summary) |
| `meeting_types.csv` | Lookup: type_id → type_name |
| `documents.csv` | One PDF per meeting (title, file_name, file_url, doc_date) |
| `agenda_items.csv` | One row per agenda item — title, body, action_type, category_id, motion text, vote result, applicant, application_id, district, project matches, confidence |
| `agenda_categories.csv` | Lookup: category_id → category_name |
| `agenda_item_projects.csv` | Join table item ↔ project |
| `agenda_item_locations.csv` | Join table item ↔ location |
| `projects.csv` | Recurring named projects (BERT Trail, Septic-to-Sewer, …) |
| `locations.csv` | Catalogue of distinct named locations with coordinates |
| `legacy_locations.csv` | Mirror of `locations.csv` for downstream tools still on the legacy schema |
| `motions.csv` | Extracted motion lines with proposer, seconder, outcome |
| `boards.csv`, `meeting_formats.csv` | Reference dimensions for the relational model |

### `silver/v2/` — wider resolver-detail variants

| File | What it holds |
|---|---|
| `locations_v2.csv` | One row per (item, location) with **typed resolver output** — `location_type`, `resolution_notes`, `geocode_confidence`, raw and normalized address, lat/lon |
| `meetings_v2.csv`, `documents_v2.csv` | Wider variants of `core/meetings.csv` / `core/documents.csv` |

### `gold/` — publication-ready deliverables

| File | What it holds |
|---|---|
| `meetings_ai_public.csv` | Flat AI-ready corpus, one row per agenda item (52 columns incl. `CitationText`, `AiReady`, primary location). This is the file the rag-arcgis-chatbot backend syncs to `backend/data/data.csv`. Regenerate standalone with `python pipeline/export_gold.py` |

### `gold/arcgis/` — map exports + per-category layers

| File | What it holds |
|---|---|
| `arcgis_agenda_map_data.csv` | Agenda-level map data — one row per (item, location), full popup fields |
| `arcgis_map_data.csv` | Legacy meeting-level map data (one row per meeting location) |
| `layers/<category>.csv` | The same agenda rows split by category into one file per ArcGIS layer (Residential, Commercial & Mixed-Use, Industry/Mining/Agriculture, Transportation, Utilities/Stormwater/Environment, Public Facilities, Budget/Contracts, Meetings/Records) |
| `arcgis_missing_coordinates.csv` | Agenda rows that need a geocode added before they can be mapped |

### `bronze/` — hand-curated inputs

| File | What it holds |
|---|---|
| `geocoded_locations.csv` | **Hand-curated geocode overrides** — read (never regenerated) by every build; do not delete |

### `silver/review/` — human QA triage

| File | What it holds |
|---|---|
| `location_candidates.csv` | Address candidates discovered during extraction that still need geocoding |
| `unmapped_agenda_items.csv` | Items the pipeline classified but couldn't place on the map |
| `extraction_review.csv` | Items flagged for human QA (OCR fallback, missing date, weak match) |
| `location_verification.csv` | Output of `pipeline/verify.py` — Lee County parcel cross-check per map point |
