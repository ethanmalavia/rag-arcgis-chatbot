# Architecture & QA Review

Static review of `rag-arcgis-chatbot` (Jul 2026). Covers product features, architectural risks, and QA posture.

## Architecture overview

```text
Question → Router
  ├─ structured  → pandas filters (counts, year, status, location)
  ├─ keyword     → ApplicationID / minutes / token search
  ├─ mixed       → keyword first, else RAG
  └─ rag         → BM25 + FAISS (RRF) → reranker → CRAG → Gemini/Groq
```

| Layer | Path | Role |
|--------|------|------|
| Backend API | `backend/` | FastAPI — `/chat`, `/chat/stream`, `/health`, `/ready`, `/warmup`, `/load` |
| Frontend | `frontend/` | Vanilla JS chat + ArcGIS webmap |
| Pipeline | `pipeline/` | PDF → medallion CSVs → `backend/data/gold/meetings_ai_public.csv` |
| Docker | `docker-compose.yml` + `backend/Dockerfile` | API (baked FAISS) + nginx static UI |

## Product features

| Feature | Detail |
|---------|--------|
| Router-first Q&A | structured \| keyword \| mixed \| rag paths |
| Structured filters | pandas counts / year / status / location |
| Keyword lookup | ApplicationID / minutes / token search |
| Hybrid RAG | BM25 + FAISS (RRF) → reranker → CRAG → LLM |
| LLM collaborate | Gemini extract + Groq summary with solo fallback |
| Recency boost + stale notices | Prefer recent meetings; warn on old sources |
| SSE streaming | `POST /chat/stream` (`meta` → `token` → `done`) |
| Health / ready / warmup | Liveness, readiness, model warmup endpoints |
| CSV `/load` (dev) | Upload replacement corpus + rebuild index |
| Static frontend serve | `SERVE_FRONTEND` mounts frontend from API |
| ArcGIS webmap UI | Vanilla JS chat + map in `frontend/` |
| PDF → medallion pipeline | `pdfs/` → bronze/silver/gold CSVs |
| Weekly autonomous refresh | `pipeline-refresh.yml` scrape + rebuild + commit |
| Rebuild-up-to-date CI guard | `pipeline-ci.yml` fails drifted deliverables |
| Drift watch | `pipeline-drift-watch.yml` |
| Cloud Run deploy | `deploy.yml` opt-in via `ENABLE_DEPLOY` |
| Index cache invalidation | CSV MD5 + embedding model in `manifest.json` |
| Optional OTEL tracing | `OTEL_ENABLED` + `requirements-eval.txt` |

## Findings

### Critical

1. **Unauthenticated `/load` can replace the corpus** (path traversal via `file.filename`) — `backend/app.py` `load_csv`; deploy `--allow-unauthenticated`.
2. **Public `/chat` with no auth or rate limits** — cost/DoS vector — `models.ChatRequest`; `deploy.yml`.

### High

3. **API keys injected as plain Cloud Run env vars** — `deploy.yml` `--set-env-vars`.
4. **CORS `allow_origins=["*"]` with `allow_credentials=True`** — `backend/app.py`.
5. **XSS via `marked.parse` → `innerHTML` without sanitization** — `frontend/app.js` `formatProse`; legacy `appendMsg` fallback.
6. **In-memory global store; `/load` not replicated across instances** — `store.py` `_store`; `max-instances: 5`.
7. **RAG/LLM path untested in CI** — no `hybrid_retrieve` / `answer_rag` E2E.

### Medium

8. **FAISS `allow_dangerous_deserialization=True`** — `backend/store.py`.
9. **Pipeline TLS verification disabled** (`ssl.CERT_NONE`) — `discover.py`, `verify.py`, `location_resolver.py`.
10. **500 responses leak exception strings** — `app.py` chat/stream/load.
11. **No question `max_length` on `ChatRequest`** — `backend/models.py`.
12. **Fragile SSE vs `/chat` contract** (Cloud Run buffers SSE) — frontend prefers `/chat`.
13. **Docs vs deploy drift on `min-instances`** — `DEPLOY_DOCKER.md` says 0; `deploy.yml` uses 1.
14. **No server-side grounding check** that URLs/IDs exist in retrieved context — `rag_path.py` prompts only.
15. **RAGAS eval job unreachable** — `ci.yml` gated on `workflow_dispatch` but workflow has no such trigger.
16. **Dockerfile runs as root** — no non-root `USER`.

### Low

17. **Hardcoded ArcGIS webmap ID** — `frontend/app.js` `WEBMAP_ID`.
18. **Dead/legacy APIs** — `should_escalate`, `invoke_llm`, unused `session_id`.
19. **Regex router is a quality SPOF** for misroutes — `backend/router.py`.
20. **nginx web service has no healthcheck** — `docker-compose.yml`.

## QA / reliability coverage

| Suite | Status | Notes |
|-------|--------|-------|
| backend smoke | Present | Routes, JSON parse, LLM tier, recency, stale, keyword |
| backend golden | Thin | 5 cases — router + 1 keyword + 1 structured count |
| pipeline parsers | Strong | ~90 tests in `test_pipeline_parsers.py` |
| pipeline discover/location | Present | `test_discover.py` + `test_location_type.py` |
| RAG / hybrid_retrieve | Missing | No integration test with index |
| LLM collaborate / SSE | Missing | No mocked LLM or stream tests |
| API TestClient | Missing | No `/chat` auth or `/load` rejection tests |
| Frontend | Missing | No unit or e2e tests |
| Type checking | Missing | Ruff + compileall only; no mypy/pyright |
| RAGAS eval | Dead | `ci.yml` job gated on unreachable `workflow_dispatch` |

## What's solid

- Clear router-first design — structured/keyword skip LLM for counts and app IDs
- Hybrid retrieval: FAISS + BM25 RRF, reranker, recency boost, stale-source notices
- LLM resilience: Gemini+Groq collaborate with solo fallback
- Schema aliases handle CSV column evolution
- Index cache invalidation via CSV MD5 + embedding model in manifest
- Pipeline medallion layout + rebuild-up-to-date CI + weekly refresh + drift watch
- Docker cold-start hygiene: models + FAISS baked; `/ready` + warmup; API healthcheck
- Frontend UX: AbortController timeouts, stream fallback, static cache middleware

## Recommended fix order

1. **Lock down the public surface**
   - Disable or auth-gate `/load`; sanitize upload filenames
   - Rate-limit `/chat`; max question length; prefer Cloud Armor
   - Move keys to Secret Manager; tighten CORS origins
   - Sanitize Markdown before `innerHTML`

2. **Close the QA gap on the money path**
   - Integration tests for `hybrid_retrieve` + `answer_rag` (mocked LLM)
   - TestClient coverage for `/chat` and `/load` rejection in prod
   - Wire or delete the unreachable RAGAS `workflow_dispatch` job
   - Align `DEPLOY_DOCKER.md` `min-instances` with `deploy.yml`

## Bottom line

The RAG design is thoughtful for an MVP — router-first paths, hybrid retrieval, and a serious data pipeline with drift guards. The glaring issues are operational security on a public Cloud Run surface and the fact that the LLM answer path is barely covered by tests.
