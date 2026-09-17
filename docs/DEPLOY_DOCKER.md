# Docker & Google Cloud Run deployment

This guide covers **free/low-cost** local Docker and **Google Cloud Run** (scale-to-zero)
for the RAG chatbot.

---

## Part 1 — Local Docker (free)

### Prerequisites

- [Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/) (Windows)
- Anthropic API key: https://console.anthropic.com/

### One-command stack

```powershell
cd T:\eagleGIS\rag-arcgis-chatbot

# 1. Create env file (once)
copy backend\.env.example backend\.env
# Edit backend\.env → set ANTHROPIC_API_KEY=sk-ant-...

# 2. Build and run (first time: 15–25 min)
docker compose up --build
```

| URL | Service |
|-----|---------|
| http://localhost:3000 | React frontend (nginx) |
| http://localhost:8080/health | API health |
| http://localhost:8080/docs | Swagger UI |
| http://localhost:3000/admin.html | Admin console |

Compose builds **`frontend-react`** (not the legacy vanilla `frontend/` chat UI) and points it at `http://localhost:8080`.

### Useful commands

```powershell
docker compose up -d          # run in background
docker compose logs -f api    # follow API logs
docker compose down           # stop
docker compose build --no-cache api   # force full rebuild
```

### Troubleshooting

| Issue | Fix |
|-------|-----|
| Build runs out of memory | Docker Desktop → Settings → Resources → **8 GB+ RAM** |
| `ANTHROPIC_API_KEY` missing | Set in `backend/.env` |
| `/ready` fails on startup | Wait 1–2 min; index loads at container start |
| Frontend can't reach API | Confirm compose build arg `VITE_API_BASE=http://localhost:8080` |

### API-only (no compose)

Serves the React SPA from the same container (`SERVE_FRONTEND=true`):

```powershell
docker build -f backend/Dockerfile -t rag-arcgis-chatbot:latest .
docker run --rm -p 8080:8080 --env-file backend\.env rag-arcgis-chatbot:latest
```

---

## Part 2 — Google Cloud Run (free tier friendly)

Cloud Run charges **only when handling requests** if you use **scale to zero**
(`--min-instances 0`). Our GitHub deploy workflow uses this setting.

Typical free tier (check current Google pricing):

- Millions of requests/month included
- CPU/memory billed per request-second
- **$0 when idle** with min-instances 0

You still need a billing account on GCP, but a low-traffic demo often stays near **$0**.

### Step 1 — Create a Google Cloud project

1. Open https://console.cloud.google.com/
2. **Select project** → **New project** → name e.g. `engage-estero-chat`
3. Note the **Project ID** (e.g. `engage-estero-chat-123456`)
4. **Billing** → link a billing account (required for Cloud Run)

### Step 2 — Install Google Cloud CLI

https://cloud.google.com/sdk/docs/install

```powershell
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
```

### Step 3 — Enable APIs

```powershell
gcloud services enable `
  run.googleapis.com `
  artifactregistry.googleapis.com `
  iamcredentials.googleapis.com `
  cloudresourcemanager.googleapis.com
```

### Step 4 — Create Artifact Registry (Docker image storage)

```powershell
$REGION = "us-central1"
gcloud artifacts repositories create rag-repo `
  --repository-format=docker `
  --location=$REGION `
  --description="RAG chatbot images"
```

### Step 5 — Build and push your image

From the repo root (Docker Desktop running):

```powershell
$PROJECT = "YOUR_PROJECT_ID"
$REGION = "us-central1"
$IMAGE = "$REGION-docker.pkg.dev/$PROJECT/rag-repo/rag-arcgis-chatbot:v1"

gcloud auth configure-docker "$REGION-docker.pkg.dev" --quiet

docker build -t $IMAGE backend
docker push $IMAGE
```

### Step 6 — Deploy to Cloud Run (scale to zero = cheapest)

```powershell
gcloud run deploy rag-arcgis-chatbot `
  --image $IMAGE `
  --region $REGION `
  --platform managed `
  --allow-unauthenticated `
  --port 8080 `
  --memory 2Gi `
  --cpu 1 `
  --min-instances 0 `
  --max-instances 3 `
  --timeout 300 `
  --set-env-vars "ANTHROPIC_API_KEY=YOUR_ANTHROPIC_KEY,SERVE_FRONTEND=false"
```

Save the **Service URL** from the output, e.g.:

`https://rag-arcgis-chatbot-abc123-uc.a.run.app`

### Step 7 — Verify

```powershell
curl https://YOUR-SERVICE-URL/health
curl https://YOUR-SERVICE-URL/ready
```

Open `https://YOUR-SERVICE-URL/docs` and test `POST /chat`.

### Agent-sync MCP (Streamable HTTP)

The same Cloud Run service exposes **engage-estero-sync** at `/mcp` when
`ENABLE_MCP_HTTP=true` (set in the Dockerfile + `deploy.yml`).

```powershell
# Unauthorized without bearer
curl -i https://YOUR-SERVICE-URL/mcp

# Health flag
curl -s https://YOUR-SERVICE-URL/health | findstr mcp_http
```

Point Cursor at the remote MCP:

```powershell
$env:ENGAGE_MCP_URL = "https://YOUR-SERVICE-URL/mcp"
$env:ENGAGE_MCP_TOKEN = "<ADMIN_API_KEY or MCP_API_KEY>"
```

Enable **engage-estero-sync-cloud** under Customize → MCPs. Seed files live in
`agent-sync/` (baked into the image); runtime writes use `/tmp/agent-sync`
(ephemeral — commit `agent-sync/` for durable handoffs).

### Step 8 — Connect the frontend

Edit `frontend/config.js` (or deploy frontend to GitHub Pages):

```javascript
window.API_BASE = "https://YOUR-SERVICE-URL";
```

Host `frontend/` on GitHub Pages, Netlify, or any static host.

---

## Part 3 — Automatic deploy from GitHub (optional)

After manual deploy works, enable CI/CD in
https://github.com/krocks9903/rag-arcgis-chatbot.

### A. Service account for deploy

```powershell
$PROJECT = "YOUR_PROJECT_ID"
$SA = "github-deploy"

gcloud iam service-accounts create $SA --display-name="GitHub Actions deploy"

$SA_EMAIL = "$SA@$PROJECT.iam.gserviceaccount.com"

gcloud projects add-iam-policy-binding $PROJECT `
  --member="serviceAccount:$SA_EMAIL" `
  --role="roles/run.admin"

gcloud projects add-iam-policy-binding $PROJECT `
  --member="serviceAccount:$SA_EMAIL" `
  --role="roles/artifactregistry.writer"

gcloud projects add-iam-policy-binding $PROJECT `
  --member="serviceAccount:$SA_EMAIL" `
  --role="roles/iam.serviceAccountUser"
```

### B. Workload Identity Federation (no JSON keys)

Follow Google's guide for GitHub Actions:
https://github.com/google-github-actions/auth/blob/main/docs/TAKE_ACTIONS.md

Summary:

1. Create a **Workload Identity Pool** + **Provider** for `github.com`
2. Restrict to repo `krocks9903/rag-arcgis-chatbot`
3. Allow the service account to impersonate from that provider

```powershell
# Example — adjust pool/provider names to match Google's guide
gcloud iam service-accounts add-iam-policy-binding $SA_EMAIL `
  --role="roles/iam.workloadIdentityUser" `
  --member="principalSet://iam.googleapis.com/projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/POOL_ID/attribute.repository/krocks9903/rag-arcgis-chatbot"
```

Copy the **provider resource name** for GitHub secret `GCP_WORKLOAD_IDENTITY_PROVIDER`.

### C. GitHub repository settings

**Settings → Secrets and variables → Actions**

**Variables**

| Name | Value |
|------|--------|
| `ENABLE_DEPLOY` | `true` |
| `GCP_PROJECT_ID` | your project id |
| `GCP_REGION` | `us-central1` |
| `AR_REPO` | `rag-repo` |
| `SERVICE_NAME` | `rag-arcgis-chatbot` |

**Secrets**

| Name | Value |
|------|--------|
| `GROQ_API_KEY` | your Groq key |
| `GEMINI_API_KEY` | your Gemini key (collaborate mode) |
| `ADMIN_API_KEY` | long random secret for `/admin.html` and `/load` |
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | full WIF provider path |
| `GCP_SERVICE_ACCOUNT` | `github-deploy@PROJECT.iam.gserviceaccount.com` |

Push to `main` (with changes under `backend/`) or run **Deploy backend to Cloud Run**
manually from the Actions tab.

---

## Cost tips

| Setting | Free-friendly | Always-on (costly) |
|---------|---------------|---------------------|
| `min-instances` | **0** | 1+ |
| `memory` | 2Gi (needed for ML) | same |
| `cpu` | 1 | 2 |
| Traffic | demo / class project | production |

Our `deploy.yml` uses **`min-instances 0`** and **`cpu 1`** for the free tier.

First request after idle may take **10–30 seconds** (cold start). The Docker image
pre-bakes the FAISS index to keep this as fast as possible.

---

## Checklist

- [ ] `backend/.env` with `ANTHROPIC_API_KEY` (and `ADMIN_API_KEY` for admin)
- [ ] `docker compose up --build` works locally
- [ ] GCP project + billing + APIs enabled
- [ ] Image pushed to Artifact Registry
- [ ] Cloud Run deploy returns `/ready` OK
- [ ] `frontend/config.js` → Cloud Run URL
- [ ] (Optional) GitHub WIF + `ENABLE_DEPLOY=true`
- [ ] GitHub secret `ADMIN_API_KEY` set before Cloud Run deploy (required by `deploy.yml`)
