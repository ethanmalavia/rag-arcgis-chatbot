# Engage Estero — agent conventions

Shared rules for Nolan, Ethan, Krish, and Cursor agents. Prefer this over inventing new process.

## Repo map

| Area | Path |
| --- | --- |
| FastAPI backend | `backend/` |
| React Pulse UI | `frontend-react/` |
| Data pipeline | `pipeline/` |
| Events aggregator | `backend/events_sources/` |
| Manual Pulse seeds | `frontend-react/public/meetings.json`, `community-events.json` |
| Agent sync state | `agent-sync/` |

## Git / identity

- Prefer commits as **noleysc** for Nolan QA work.
- Only commit when asked; only push when asked.
- Never amend pushed commits; never `--force` on `main`.
- Do not commit secrets, `.env`, or the stakeholder PPTX unless explicitly requested.

## Backend

- Community events: soft-fail per source; geo-filter + dedupe in `normalize.py`.
- Hertz Arena: `events_sources/hertz.py` from the public upcoming-events listing only.
- Events chat path must not steal planning/zoning questions (`backend/events_path.py`).
- Document type: `from langchain_core.documents import Document` (not `langchain.schema`).
- Tests: `cd backend && pytest -q` (CI uses Python 3.11).

## Frontend

- Preserve existing Pulse design system; no generic AI purple/cream redesigns.
- Manual JSON files are the fallback when APIs are missing — keep dates future-dated.

## Coordination (MCP)

1. Session start (non-trivial work): **`get_session_brief`** once.
2. Before editing a hot area: `claim_area` (soft lock).
3. Session end / unfinished work: `write_handoff`, `release_area` if claimed, `update_shared_context` when priorities or blockers change.
4. Commit `agent-sync/**` with related work so teammates pull the same state.

Use `get_handoff` / `list_handoffs` for older notes; `get_conventions` only when process rules are unclear.

stdio MCP is per machine — **git is the sync bus** for durable state. Cloud Run serves the same tools at `/mcp` (Bearer auth); runtime writes are ephemeral under `/tmp/agent-sync` until you commit `agent-sync/`.
