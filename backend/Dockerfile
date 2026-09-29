# Container image for Ask Engage Estero (FastAPI + FAISS + React UI).
# Build from repo root: docker build -f backend/Dockerfile -t rag-arcgis-chatbot .
#
# The public UI is frontend-react (hero refresh, Pulse events, Instant App map).
# Admin stays on the legacy vanilla pages, copied into the React dist.

# ─── React production build ─────────────────────────────────────────────
FROM node:22-alpine AS frontend-build
WORKDIR /web
COPY frontend-react/package.json frontend-react/package-lock.json ./
RUN npm ci
COPY frontend-react/ ./
# Same-origin API when the SPA is served by FastAPI.
ENV VITE_API_BASE=
RUN npm run build

# Admin HTML/JS/CSS ship via frontend-react/public/ → dist/ during Vite build.
# (No extra COPY needed; keep logo assets that public/ already includes.)

# ─── API + baked index ──────────────────────────────────────────────────
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    HF_HOME=/app/backend/.cache/huggingface \
    TRANSFORMERS_CACHE=/app/backend/.cache/huggingface \
    SENTENCE_TRANSFORMERS_HOME=/app/backend/.cache/sentence-transformers \
    EMBEDDING_MODEL=BAAI/bge-small-en-v1.5 \
    RERANKER_MODEL=cross-encoder/ms-marco-MiniLM-L-6-v2 \
    SERVE_FRONTEND=true \
    ENABLE_MCP_HTTP=true \
    AGENT_SYNC_SEED_DIR=/app/agent-sync \
    AGENT_SYNC_DIR=/tmp/agent-sync

WORKDIR /app/backend

COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ .
COPY agent-sync /app/agent-sync
COPY tools/engage-estero-mcp /app/tools/engage-estero-mcp
COPY --from=frontend-build /web/dist /app/frontend

# Pre-download models and bake FAISS + BM25 index for fast Cloud Run cold starts.
# get_reranker() also bakes the validated ONNX export (backend/onnx_reranker/).
RUN mkdir -p /app/backend/.cache/huggingface /app/backend/.cache/sentence-transformers \
    && python -c "from store import build_store; from config import DEFAULT_CSV_PATH; build_store(DEFAULT_CSV_PATH); from retrieval import get_reranker; get_reranker()"

EXPOSE 8080

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
