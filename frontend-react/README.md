# Ask Engage Estero — React frontend

Vite + React 18 + TypeScript rewrite of the Engage Estero civic chatbot frontend. Talks to the
existing FastAPI backend in `../backend` — that service is unchanged; only the UI moved.

## Run it

```bash
npm install
npm run dev
```

This starts the Vite dev server (default `http://localhost:5173`). Make sure the backend is
running separately:

```bash
cd ../backend
uvicorn app:app --reload
```

## Configuration

The backend base URL is read from the `VITE_API_BASE` env var at build time, falling back to
`http://localhost:8000` if unset. Copy `.env.example` to `.env.local` and edit as needed:

```bash
cp .env.example .env.local
```

```
VITE_API_BASE=http://localhost:8000       # local dev (default)
VITE_API_BASE=   # empty = same-origin (VPS / Docker production)
```

## Scripts

- `npm run dev` — start the dev server with HMR
- `npm run build` — type-check (`tsc -b`) and produce a production build in `dist/`
- `npm run preview` — serve the production build locally
- `npm run lint` — run oxlint

## Structure

```
src/
  components/
    TopBar/        logo, health status, live record badge, new chat, mobile map toggle
    ChatPanel/      Hero, MessageList, Message, ProjectCard, ArticleCard, SourcesList,
                    TypingIndicator, ChatInput, DatasetBar
    RightPanel/     Map | Pulse tab strip — keeps MapPanel mounted (display:none) when
                    switched away from, so the ArcGIS view never re-initializes
    MapPanel/       ArcGIS WebMap/MapView wrapper
    Dashboard/      Community Pulse: NextMeetings, LatestNews, RecentDecisions widgets
  hooks/
    useChat.ts      send/streaming (SSE with fallback to /chat), message state
    useHealth.ts    polls GET /health every 15s
    useAutoScroll.ts  sticks to bottom unless the user scrolls up
    useMeetings.ts  loads public/meetings.json, filters to upcoming occurrences
    useNews.ts      fetches latest 5 EsteroToday posts (direct browser fetch — see below)
    useRecentDecisions.ts  fetches GET /recent-decisions
  lib/
    parseAnswer.ts  JSON-block card extraction, legacy delimiter fallback, prose cleanup
    mapViewStore.ts singleton access to the live MapView (Directions, Show-on-map panning)
    uiStore.ts      singleton for switching the Map/Pulse tab from outside RightPanel
                    (used by the board card's "Show on map" link)
    config.ts       API_BASE resolution
public/
  meetings.json     manually maintained meeting schedule — see the file's own _comment
                    field for the update process and source of truth
```

## Community Pulse dashboard

The right panel is now a **Map | Pulse** tab strip. The Map tab is unchanged and stays
mounted at all times — switching to Pulse just hides it with `display:none` rather than
unmounting, since re-creating the ArcGIS `MapView` is expensive and would lose pan/zoom state.

Three widgets on the Pulse tab:
- **Next Meetings** — reads `public/meetings.json`, a **manually maintained** file. Update it
  monthly: drop past dates, add the next occurrence(s), and verify against
  https://estero-fl.gov/agendas-minutes/. Cadence notes live in the file's `_comment` field.
- **Latest from EsteroToday** — fetches the 5 newest posts from EsteroToday's public WordPress
  REST API (`useNews.ts`).
- **Upcoming Events** — `GET /api/events` aggregates EsteroToday's Events Calendar (all
  categories), FGCU Athletics ICS (local games), Lee County Parks calendar, Visit Fort Myers
  tourism events, and optional `public/community-events.json`. Filter chips: All / Government /
  Music / Markets / Sports / Fairs / Community. Mini-calendar merges these with Village meetings.

Chat questions like "what's happening this weekend?" or "any concerts?" take a structured
**events** path (no RAG) and return short bullets from the same feed.

A gold notification dot appears on the Pulse tab when a meeting in `meetings.json` falls within
the next 7 days.

## Notes

- Uses `@arcgis/core` ES modules (not the CDN AMD build), so there's no `marked.js`/AMD
  ordering issue to worry about — `react-markdown` renders bot prose.
- `POST /chat/stream` is called first; if the backend doesn't implement it (404/network error),
  it falls back to `POST /chat` transparently.
- Board cards include `lat`/`lng` when the source CSV has coordinates (~76% of records). When
  present, a "Show on map" button appears and switches to the Map tab + pans there directly
  (no geocoding round-trip, unlike the address-based "Directions" button).
- The old vanilla frontend in `../frontend` is untouched and still works standalone.
