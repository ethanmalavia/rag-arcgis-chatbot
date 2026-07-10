// Same-origin when the UI is served with the API (Cloud Run, single-container local).
// Split docker-compose stack: nginx on :3000 talks to API on :8080.
if (
  typeof window !== "undefined" &&
  (window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1") &&
  window.location.port === "3000"
) {
  window.API_BASE = "http://localhost:8080";
}
