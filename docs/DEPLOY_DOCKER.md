# Docker deployment (local + VPS)

Ask Engage Estero runs as one Docker image: FastAPI + the React Pulse UI, with the
FAISS index baked at build time.

**Production is the VPS**, not Google Cloud Run or GitHub Pages. Those hosts are
retired for this product. Public URL: https://tracker.esterotoday.com/

## Part 1 — Local Docker

```bash
cp backend/.env.example backend/.env   # set ANTHROPIC_API_KEY
docker compose up --build
```

- React UI: http://localhost:3000
- API: http://localhost:8080/docs
- Admin: http://localhost:3000/admin.html

`docker-compose.yml` runs API + web as separate services for local work.
Production uses the single-image layout under `deploy/vps/`.

## Part 2 — Production VPS

Installer and update scripts live in **`deploy/vps/`**:

| File | Purpose |
|------|---------|
| `install.sh` | First-time install (Docker if missing, `.env`, compose build) |
| `update.sh` | `git pull` + rebuild (also `/usr/local/bin/update-engage-estero`) |
| `docker-compose.yml` | Binds **`127.0.0.1:8080`** only |
| `SECURITY.md` | Hardening checklist |
| `.env.example` | Keys template (never commit `.env`) |

One-time install on Ubuntu/Debian as root:

```bash
curl -fsSLo /tmp/install-engage-estero.sh \
  https://raw.githubusercontent.com/noleysc/rag-arcgis-chatbot/main/deploy/vps/install.sh
bash /tmp/install-engage-estero.sh
```

Put Plesk (or nginx) TLS in front of `http://127.0.0.1:8080/`. Do not expose 8080
publicly. Do not replace esterotoday.com — use the tracker subdomain.

GitHub Actions (`deploy-vps.yml`) rebuilds the box on the self-hosted
`engage-estero` runner via `sudo -n update-engage-estero`.

## Retired hosts

| Host | Status |
|------|--------|
| Cloud Run `rag-arcgis-chatbot` | Deleted |
| GitHub Pages | Not used (workflow removed) |

If an old `.run.app` URL still appears in bookmarks or Cursor MCP config, point it
at https://tracker.esterotoday.com/ (and `/mcp` with Bearer `MCP_API_KEY` when enabled).
