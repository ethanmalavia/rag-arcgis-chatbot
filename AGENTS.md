# Engage Estero — Ask chatbot (`rag-arcgis-chatbot`)

## Stack

- Backend: FastAPI under `backend/`
- Frontend: React Pulse under `frontend-react/`
- Pipeline: `pipeline/` (Lee County / deliverables)

## Team

| Person | Focus |
| --- | --- |
| Nolan (`noleysc`) | QA, tests, events sources, release checklist |
| Ethan | Backend RAG, retrieval, chat routing |
| Krish | Frontend / Pulse UI |

## Coordination

Use the **`engage-estero-sync`** MCP (`.cursor/mcp.json` → local stdio or Cloud Run `/mcp`).

**Session start:** `get_session_brief` once. **Session end:** `write_handoff`, `release_area`, `update_shared_context` as needed.

Other tools: `get_handoff`, `list_handoffs`, `claim_area`, `get_conventions`, `get_shared_context` (full dump).

Shared state lives in `agent-sync/` (git). Commit after MCP updates so teammates stay in sync.

## Hard constraints

- Events chat must not hijack planning/zoning questions.
- Soft-fail third-party event scrapers; Hertz uses the public upcoming-events page only — do not add brittle Amerant scrapers.
- Prefer `langchain_core.documents.Document` over `langchain.schema`.
- Only commit/push when the human asks.
