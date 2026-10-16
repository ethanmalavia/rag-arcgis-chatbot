# VPS security checklist (Ask Engage Estero)

Public site: https://tracker.esterotoday.com/  
App bind: `127.0.0.1:8080` only (Plesk proxies TLS). Do not publish 8080 on the internet.

## On the server (root)

- [ ] `deploy/vps/.env` is mode `600` and owned by root (`ls -l deploy/vps/.env`)
- [ ] Compose still maps `127.0.0.1:8080:8080` (not `0.0.0.0:8080`)
- [ ] SSH key login works from your laptop before you disable passwords
- [ ] Optional SSH key expiry: `expiry-time="20270101"` prefix on the authorized_keys line
- [ ] `apt update && apt upgrade -y` on a schedule (or unattended-upgrades)
- [ ] Runner user `gha` can only run `sudo -n update-engage-estero` (see `/etc/sudoers.d/gha-deploy`)
- [ ] Do not copy `.env` off the box; rotate `ADMIN_API_KEY` / `MCP_API_KEY` if leaked

## App settings

Keep these in `deploy/vps/.env`:

```
ENABLE_RATE_LIMIT=true
ENABLE_MCP_HTTP=true
```

Rate limits are in-memory per container (chat ~40 req/IP/min by default). They blunt casual abuse; they are not a WAF.

## What GitHub Actions watches

- `health-watch.yml` — `/health` and `/ready` on tracker every 30 minutes
- `deploy-vps.yml` — rebuilds only when app paths change, on the `engage-estero` runner

## Do not

- Proxy `esterotoday.com` itself to this app (tracker subdomain only)
- Re-enable Cloud Run or GitHub Pages for this product
- Add brittle venue scrapers that fail hard (soft-fail per source)
