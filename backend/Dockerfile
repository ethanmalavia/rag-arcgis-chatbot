# Container image for the RAG ArcGIS chatbot backend (FastAPI + FAISS + Groq).
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    HF_HOME=/app/.cache/huggingface \
    TRANSFORMERS_CACHE=/app/.cache/huggingface \
    SENTENCE_TRANSFORMERS_HOME=/app/.cache/sentence-transformers \
    EMBEDDING_MODEL=BAAI/bge-small-en-v1.5 \
    RERANKER_MODEL=BAAI/bge-reranker-base \
    SERVE_FRONTEND=false

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Pre-download models and bake FAISS + BM25 index for fast Cloud Run cold starts.
RUN mkdir -p /app/.cache/huggingface /app/.cache/sentence-transformers \
    && python -c "from store import build_store; from config import DEFAULT_CSV_PATH; build_store(DEFAULT_CSV_PATH)"

EXPOSE 8080

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
