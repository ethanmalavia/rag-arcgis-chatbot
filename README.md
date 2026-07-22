# Ask Engage Estero / RAG ArcGIS Chatbot

Router-first Q&A for Estero planning & zoning records: structured filters, keyword lookup, and corrective RAG with hybrid retrieval. Includes a vanilla chat UI, a React + Community Pulse frontend, an administrator console, and a public “report incorrect location / suggest a change” flow. The EagleGIS PDF extraction pipeline in this repo produces the chatbot corpus.

## What you need

- Python 3.11 **or** Docker Desktop
- Groq API key (`GROQ_API_KEY`); Gemini optional for collaborate mode
- Optional: `ADMIN_API_KEY` for `/admin` and CSV `/load`
- Optional: GCP account for Cloud Run deploy
- Pipeline rebuild: Tesseract OCR (`apt install tesseract-ocr` on Linux)
- React UI: Node 18+ (`frontend-react/`)

## Quick start (Docker — recommended)

```powershell
cd T:\eagleGIS\rag-arcgis-chatbot
copy backend\.env.example backend\.env   # add GROQ_API_KEY (and ADMIN_API_KEY)
docker compose up --build
```

- Frontend: http://localhost:3000  
- API: http://localhost:8080/docs  
- Admin: http://localhost:3000/admin.html  

Full local + Google Cloud instructions: **[docs/DEPLOY_DOCKER.md](docs/DEPLOY_DOCKER.md)**

## Quick start (Python only)

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env   # add GROQ_API_KEY (and ADMIN_API_KEY)
uvicorn app:app --reload --port 8000
```

Open http://localhost:8000 — the API serves the vanilla frontend when `SERVE_FRONTEND=true` (default). Admin: http://localhost:8000/admin.html

### React frontend (Vite)

```powershell
cd frontend-react
npm install
npm run dev
```

Serves on http://localhost:5173 and talks to the API via `VITE_API_BASE` (default `http://localhost:8000` — see `frontend-react/.env.example`).

## Pipeline architecture

```text
Question → Router
  ├─ structured  → pandas filters (counts, year, status, location)
  ├─ keyword     → ApplicationID / minutes / token search
  ├─ mixed       → keyword first, else RAG
  └─ rag         → BM25 + FAISS (RRF) → reranker → CRAG → Gemini/Groq JSON
```

| Component | Default |
|-----------|---------|
| Embeddings | `BAAI/bge-small-en-v1.5` (`EMBEDDING_MODEL`) |
| Reranker | `cross-encoder/ms-marco-MiniLM-L-6-v2` (`RERANKER_MODEL`) |
| Score threshold | `0.25` |
| CRAG max iterations | `1` |

## API

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Liveness + index stats |
| `GET /ready` | Readiness (index loaded) |
| `POST /chat` | Structured JSON answer |
| `POST /chat/stream` | SSE stream (`meta` → `token` → `done`) |
| `POST /reports` | Public: report incorrect location / suggest a change |
| `GET /admin` | Redirects to administrator UI (`/admin.html`) |
| `GET /admin/status` | Admin: index + report counts (Bearer `ADMIN_API_KEY`) |
| `GET /admin/reports` | Admin: list user reports |
| `PATCH /admin/reports/{id}` | Admin: update report status |
| `POST /load` | Admin: upload replacement CSV + rebuild index |

Set `ADMIN_API_KEY` in `backend/.env`, then open **/admin.html** and sign in with that key. Public users submit reports from the chat **Report** button (vanilla and React).

## Project structure

```text
rag-arcgis-chatbot/
├── .github/workflows/          # lint, pytest, pipeline CI/refresh, deploy
├── backend/
│   ├── app.py                  # FastAPI + admin/reports + optional static UI
│   ├── admin_auth.py / reports.py
│   ├── store.py                # FAISS + BM25 index
│   ├── data/gold/meetings_ai_public.csv
│   ├── data/esterotoday_content.csv   # EsteroToday articles (React Pulse / experimental)
│   ├── ingest.py / diagnose_retrieval.py  # helpers from React branch (not wired into app.py)
│   └── tests/
├── frontend/                   # Vanilla JS chat + admin.html
├── frontend-react/             # Vite + React + TypeScript + Community Pulse
├── pipeline/                   # EagleGIS PDF → medallion CSVs
├── pdfs/
├── docker-compose.yml
└── docs/DEPLOY_DOCKER.md
```

## Data pipeline

The `pipeline/` directory parses meeting-minute PDFs from `pdfs/` into medallion CSVs under `backend/data/`, resolves locations against Lee County parcel data, and exports `backend/data/gold/meetings_ai_public.csv` — the file the chatbot indexes.

```bash
pip install -r pipeline/requirements.txt
python pipeline/build.py --pdf-dir pdfs --source-csv pdfs/Estero_Meetings_Final.csv --out-dir backend/data
python pipeline/verify.py
python -m pytest pipeline/tests -q
```

See [`pipeline/README.md`](pipeline/README.md) for full details.

## CI

- **ci.yml** — ruff + backend router/golden/smoke/admin tests (no Groq key required)
- **pipeline-ci.yml** — pipeline pytest + deliverables up-to-date guard
- **pipeline-refresh.yml** — weekly data refresh from source PDFs
- **deploy.yml** — Cloud Run deploy when `ENABLE_DEPLOY=true`

## Production

Cloud Run serves the vanilla frontend and API from one container (`SERVE_FRONTEND=true`). Set `ENABLE_DEPLOY=true` and GCP secrets/vars per [docs/DEPLOY_DOCKER.md](docs/DEPLOY_DOCKER.md).

## Notes

- Corpus path: `backend/data/gold/meetings_ai_public.csv` (override with `CSV_PATH`)
- Index is rebuilt when the CSV changes (hash in `faiss_index/manifest.json`)
- Optional tracing: `OTEL_ENABLED=true` + `pip install -r requirements-eval.txt`
- `frontend-react/` Community Pulse widgets (meetings/news/recent decisions) expect companion APIs or static JSON; `/recent-decisions` from the alternate backend rewrite is **not** mounted on the Schema V3 `app.py` yet
