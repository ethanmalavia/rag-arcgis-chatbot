// Production (Docker / VPS) builds with VITE_API_BASE="" so the UI
// talks to the same origin that serves the SPA. Local `npm run dev` leaves
// the var unset and falls back to the FastAPI default port.
const raw = import.meta.env.VITE_API_BASE as string | undefined;

export const API_BASE =
  raw !== undefined ? raw.trim().replace(/\/$/, "") : "http://localhost:8000";
