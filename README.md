# RAG ArcGIS Chatbot

Router-first Q&A for Estero planning & zoning records: structured filters, keyword lookup, and corrective RAG with hybrid retrieval.

## What you need

- Python 3.11
- Groq API key (`GROQ_API_KEY`)
- Optional: GCP credentials for Cloud Run deploy

## Quick start

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env   # add GROQ_API_KEY
uvicorn app:app --reload --port 8000
```

Open http://localhost:8000 — the API serves the frontend when `SERVE_FRONTEND=true` (default).

Or serve the frontend separately on port 3000 (`python -m http.server 3000` in `frontend/`).

## Pipeline architecture

```text
Question → Router
  ├─ structured  → pandas filters (counts, year, status, location)
  ├─ keyword     → ApplicationID / minutes / token search
  ├─ mixed       → keyword first, else RAG
  └─ rag         → BM25 + FAISS (RRF) → reranker → CRAG → Groq JSON
```

| Component | Default |
|-----------|---------|
| Embeddings | `BAAI/bge-small-en-v1.5` (`EMBEDDING_MODEL`) |
| Reranker | `BAAI/bge-reranker-base` (`RERANKER_MODEL`) |
| Score threshold | `0.35` |
| CRAG max iterations | `2` |

Production Docker/Cloud Run can set `EMBEDDING_MODEL=BAAI/bge-m3` for higher quality.

## API

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Liveness + index stats |
| `GET /ready` | Readiness (index loaded) |
| `POST /chat` | Structured JSON answer |
| `POST /chat/stream` | SSE stream (`meta` → `token` → `done`) |
| `POST /load` | Upload replacement CSV |

## Project structure

```text
rag-arcgis-chatbot/
├── .github/workflows/
│   ├── ci.yml           # lint + pytest + optional RAGAS
│   ├── deploy.yml       # Cloud Run (opt-in)
│   └── sync-data.yml    # weekly EagleGIS gold CSV sync
├── backend/
│   ├── app.py           # FastAPI + static frontend
│   ├── config.py
│   ├── store.py         # FAISS + BM25 index
│   ├── retrieval.py     # hybrid RRF + rerank
│   ├── router.py
│   ├── structured_path.py
│   ├── keyword_path.py
│   ├── rag_path.py      # CRAG + Groq
│   ├── data/            # data.csv (chatbot corpus) + pipeline medallion tiers
│   │   ├── bronze/      # hand-curated inputs (geocode overrides, URL lookup)
│   │   ├── silver/      # relational tables (core/, v2/) + QA triage (review/)
│   │   └── gold/        # meetings_ai_public.csv + arcgis/ map exports
│   └── tests/golden_qa.json
├── frontend/
├── pipeline/               # EagleGIS PDF-extraction pipeline (see pipeline/README.md)
│   ├── build.py            # main orchestrator
│   ├── verify.py           # Lee County parcel cross-check
│   ├── export_gold.py      # regenerate gold CSV from silver tables
│   ├── eaglegis/           # extractors, classifiers, location resolver
│   └── tests/
├── pdfs/                   # source meeting-minute PDFs + legacy Estero_Meetings_Final.csv
└── scripts/eval_ragas.py
```

## Data pipeline

The `pipeline/` directory contains the EagleGIS extraction pipeline that
produces the Estero meeting data in this repo. It parses meeting-minute
PDFs from `pdfs/` into the medallion CSVs under `backend/data/`, resolving each
agenda item to a verified (lat, lon) via Lee County parcel data, and
exports `backend/data/gold/meetings_ai_public.csv` — the corpus the chatbot
backend consumes.

```bash
pip install -r pipeline/requirements.txt
python pipeline/build.py --pdf-dir pdfs --source-csv pdfs/Estero_Meetings_Final.csv --out-dir backend/data
python pipeline/verify.py          # Lee County parcel cross-check
python -m pytest pipeline/tests -q
```

See [`pipeline/README.md`](pipeline/README.md) for CLI flags, module
internals, deliverable schemas, and the review workflow.

### How the pipeline works

New minutes are discovered automatically: `pipeline/discover.py` scrapes the
village's Council and PZDB minutes index pages, downloads any newly posted
PDF into `pdfs/`, and records its URL in the bronze lookup. The weekly
refresh workflow runs it before every rebuild, so the corpus tracks
estero-fl.gov without manual steps (committing a PDF by hand still works).

From there, `pipeline/build.py` orchestrates six stages, each backed by a
module in `pipeline/eaglegis/`:

1. **Text extraction** (`text.py`) — pulls embedded text from each PDF with
   PyMuPDF. Scanned documents with too little embedded text fall back to
   Tesseract OCR automatically.
2. **Meeting metadata** (`extractors.py`) — infers the meeting date, board
   (Village Council vs. Planning Zoning & Design Board), format (regular,
   workshop, hearing…), venue, and cancellation status from the text and
   filename.
3. **Agenda parsing** (`extractors.py`) — splits each document into numbered
   agenda items and pulls out the action taken, motion text, proposer /
   seconder, and vote result for each one.
4. **Classification** (`classifiers.py`) — assigns every item an action type
   (Ordinance, Resolution, Contract Approval…) and one of eight public-facing
   categories (Residential Development, Transportation & Mobility…), and
   matches recurring project and location names against known aliases.
5. **Location resolution** (`location_resolver.py`) — turns address text into
   a single verified (lat, lon). It classifies each reference (single parcel,
   intersection, road corridor, named venue, neighborhood…) and resolves it
   against Lee County's public parcel / road / park layers, most-precise
   resolver first. Responses are disk-cached in `.cache/leepa/`, so repeat
   builds make zero network calls.
6. **Write deliverables** (`writer.py`, `gold.py`) — emits every CSV under
   `backend/data/` in one deterministic pass. Rebuilding from the same inputs
   produces byte-identical output, which is what lets CI verify the committed
   data (see below).

`pipeline/verify.py` runs separately as a quality gate: for every plotted map
point it asks Lee County which parcel contains our coordinate and which parcel
matches our extracted address — if the two disagree, the row is flagged
`MISMATCH` and CI refuses to publish.

### What the deliverables mean

Deliverables live in `backend/data/`, tiered by how much validation they have
had (a bronze → silver → gold "medallion" layout):

| Tier | Contents | Meaning |
|------|----------|---------|
| `bronze/` | `geocoded_locations.csv`, `estero_minutes_urls.txt` | **Hand-curated inputs.** Manually verified geocode overrides and the filename → estero-fl.gov URL lookup. The build reads these but never regenerates them — treat them as source data, not output. |
| `silver/core/` | `meetings`, `agenda_items`, `motions`, `locations`, `projects`, join tables… | **The validated relational model.** One row per meeting / agenda item / motion / location, with stable IDs and foreign keys — the queryable source of truth everything else derives from. |
| `silver/v2/` | `locations_v2`, `meetings_v2`, `documents_v2` | Wider variants with full resolver detail (raw vs. normalized address, parcel ID, geocode confidence, resolution notes). |
| `silver/review/` | `extraction_review`, `unmapped_agenda_items`, `location_candidates`, `location_verification` | **Human QA triage.** Items the extractor was unsure about, items that couldn't be placed on the map, and the verifier's parcel cross-check report. These measure data quality; nothing downstream consumes them. |
| `gold/` | `meetings_ai_public.csv` | **The AI-ready corpus.** One flat row per agenda item (52 columns) with pre-built citation text, review flags, and the primary location denormalized on. This is the file the chatbot indexes. |
| `gold/arcgis/` | `arcgis_agenda_map_data.csv`, `layers/<category>.csv`, `arcgis_missing_coordinates.csv` | **Map-ready exports.** Every located agenda item with popup fields, split into one CSV per category for direct import as ArcGIS webmap layers, plus the short list of rows still needing a geocode. |

Gold is the highest tier: complete, verified, and consumable as-is — the
chatbot and the webmap only ever read gold. Silver is for anyone who needs to
query or re-derive; bronze is the part a human maintains by hand.

**Who owns the data:** the committed CSVs are canonical to the CI environment.
Every week `pipeline-refresh` scrapes estero-fl.gov for new minutes, downloads
them, rebuilds, verifies against Lee County parcels, and commits the result —
no manual step required. `pipeline-ci`'s rebuild guard fails any PR whose data
doesn't match a fresh rebuild. If you do need to add a PDF by hand, commit it
to `pdfs/` (plus its URL line in `bronze/estero_minutes_urls.txt`) and the same
flow runs — but never commit locally built CSVs.

## CI / data sync

- **CI** runs router + golden Q&A tests without a Groq key.
- **sync-data.yml** pulls `meetings_ai_public.csv` from EagleGIS every Monday.
- **RAGAS eval** (`workflow_dispatch` + `GROQ_API_KEY`): `python scripts/eval_ragas.py`
- **Pipeline workflows** (`pipeline-ci`, `pipeline-refresh`, `pipeline-drift-watch`)
  test the extraction pipeline, scrape estero-fl.gov weekly for new minutes and
  rebuild `backend/data/`, and re-verify committed coordinates against live Lee
  County parcels daily.

## Production frontend

Set before `app.js` when API is on a different host:

```html
<script>window.API_BASE = "https://your-service.run.app";</script>
```

## Notes

- Index is rebuilt when `data.csv` changes (hash in `faiss_index/manifest.json`).
- Set `SERVE_FRONTEND=false` in Cloud Run when frontend is hosted elsewhere.
- Optional tracing: `OTEL_ENABLED=true` + `pip install -r requirements-eval.txt`
