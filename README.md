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
│   ├── orchestrator.py
│   └── tests/golden_qa.json
├── frontend/
├── pipeline/               # EagleGIS PDF-extraction pipeline (see pipeline/README.md)
│   ├── build.py            # main orchestrator
│   ├── verify.py           # Lee County parcel cross-check
│   ├── export_gold.py      # regenerate gold CSV from silver tables
│   ├── eaglegis/           # extractors, classifiers, location resolver
│   └── tests/
├── pdfs/                   # source meeting-minute PDFs + legacy Estero_Meetings_Final.csv
├── data/                   # pipeline deliverables, medallion tiers
│   ├── bronze/             # hand-curated inputs (geocode overrides, URL lookup)
│   ├── silver/             # relational tables (core/, v2/) + QA triage (review/)
│   └── gold/               # meetings_ai_public.csv + arcgis/ map exports
└── scripts/eval_ragas.py
```

## Data pipeline

The `pipeline/` directory contains the EagleGIS extraction pipeline that
produces the Estero meeting data in this repo. It parses meeting-minute
PDFs from `pdfs/` into the medallion CSVs under `data/`, resolving each
agenda item to a verified (lat, lon) via Lee County parcel data, and
exports `data/gold/meetings_ai_public.csv` — the corpus the chatbot
backend consumes.

```bash
pip install -r pipeline/requirements.txt
python pipeline/build.py --pdf-dir pdfs --source-csv pdfs/Estero_Meetings_Final.csv --out-dir data
python pipeline/verify.py          # Lee County parcel cross-check
python -m pytest pipeline/tests -q
```

See [`pipeline/README.md`](pipeline/README.md) for CLI flags, module
internals, deliverable schemas, and the review workflow.

## CI / data sync

- **CI** runs router + golden Q&A tests without a Groq key.
- **sync-data.yml** pulls `meetings_ai_public.csv` from EagleGIS every Monday.
- **RAGAS eval** (`workflow_dispatch` + `GROQ_API_KEY`): `python scripts/eval_ragas.py`
- **Pipeline workflows** (`pipeline-ci`, `pipeline-refresh`, `pipeline-drift-watch`)
  test the extraction pipeline, rebuild `data/` on new PDFs, and re-verify
  committed coordinates against live Lee County parcels.

## Production frontend

Set before `app.js` when API is on a different host:

```html
<script>window.API_BASE = "https://your-service.run.app";</script>
```

## Notes

- Index is rebuilt when `data.csv` changes (hash in `faiss_index/manifest.json`).
- Set `SERVE_FRONTEND=false` in Cloud Run when frontend is hosted elsewhere.
- Optional tracing: `OTEL_ENABLED=true` + `pip install -r requirements-eval.txt`
