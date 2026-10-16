# Ask Engage Estero — Release QA Checklist

Use this before/after a VPS deploy (`deploy-vps.yml` or `sudo update-engage-estero`).
Mark each item pass/fail. File thumbs-down feedback and any fail with steps to reproduce.

## 0. Preconditions

- [ ] Deploy finished green (`Deploy to VPS` workflow) or manual `update-engage-estero` completed
- [ ] `deploy/vps/.env` on the server has `ANTHROPIC_API_KEY`, `ADMIN_API_KEY`, `MCP_API_KEY` (mode 600)
- [ ] Public URL: https://tracker.esterotoday.com/
- [ ] Optional: `health-watch.yml` last run green

## 1. Liveness (no LLM tokens)

- [ ] `GET /health` → 200, `chain_ready: true`
- [ ] `GET /ready` → 200 with `record_count` > 0
- [ ] `GET /warmup` → 200 (local retrieve + reranker; does not call Claude)
- [ ] `GET /api/events` and `GET /recent-decisions` → 200

## 2. Chat paths

| Case | Example | Expect |
|------|---------|--------|
| Keyword / app ID | `DOS2023-E005` | Project card + summary, not empty |
| Structured count | `How many records were approved in 2023?` | Count / list, route structured (no LLM) |
| Narrative RAG | `What happened with Goodwill?` | Concise bullets (≤3), cited project(s) |
| Recency | `Any new developments on Corkscrew?` | Prefers recent records; no ancient-only dump |
| Events weekend | `What's happening this weekend?` | Events bullets with dates/venues (no LLM) |
| Events vs planning | `What is happening on Corkscrew Road?` | **RAG/planning**, not calendar dump |
| Streaming | send via UI | Tokens or full answer; no blank bubble |

## 3. Community Pulse

- [ ] Next Meetings shows upcoming Council / PZDB rows
- [ ] Latest News loads EsteroToday posts
- [ ] Upcoming Events list loads (not permanent error empty)
- [ ] Filter chips: All / Government / Music / Markets / Sports / Fairs / Community / Other
- [ ] Sports chip does not bury Village meetings on All (spot-check)
- [ ] Mini-calendar dots match list for a selected day
- [ ] FGCU away games (Atlanta / Baton Rouge) do **not** appear

## 4. Map & cards

- [ ] Project with `lat`/`lng` shows **Show on map** and pans
- [ ] Directions still works for an address string
- [ ] Village Council cards render when applicable

## 5. Admin & reports

- [ ] `/admin.html` loads (same-origin API base; not forced to localhost)
- [ ] Without `ADMIN_API_KEY` header, `/load` and `/admin/status` → 401
- [ ] Public **Report** flow creates a row; admin list shows it with key

## 6. Feedback & regression signals

- [ ] Thumbs up/down on a React chat answer succeeds
- [ ] Optional: run `python scripts/summarize_feedback.py` after a few real sessions
- [ ] Optional: `python scripts/eval_quality.py --retrieve-only --limit 20`

## 7. Sign-off

| Role | Name | Date | Pass? |
|------|------|------|-------|
| QA | | | |
| Deploy owner | | | |

**Notes / defects:**
