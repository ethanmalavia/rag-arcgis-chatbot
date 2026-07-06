"""Environment-driven configuration for the RAG pipeline."""
from __future__ import annotations

import os

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(BACKEND_DIR)
FRONTEND_DIR = os.path.join(REPO_ROOT, "frontend")

INDEX_DIR = os.path.join(BACKEND_DIR, "faiss_index")
MANIFEST_FILE = os.path.join(INDEX_DIR, "manifest.json")
BM25_FILE = os.path.join(INDEX_DIR, "bm25_corpus.json")
DATA_DIR = os.path.join(BACKEND_DIR, "data")
DEFAULT_CSV_PATH = os.path.join(DATA_DIR, "data.csv")

EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
RERANKER_MODEL = os.getenv("RERANKER_MODEL", "BAAI/bge-reranker-base")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")

DENSE_K = int(os.getenv("DENSE_K", "15"))
SPARSE_K = int(os.getenv("SPARSE_K", "15"))
RERANK_K = int(os.getenv("RERANK_K", "5"))
SCORE_THRESHOLD = float(os.getenv("SCORE_THRESHOLD", "0.35"))
CRAG_MAX_ITERS = int(os.getenv("CRAG_MAX_ITERS", "2"))
CHUNK_SUMMARY_MIN = int(os.getenv("CHUNK_SUMMARY_MIN", "200"))

OTEL_ENABLED = os.getenv("OTEL_ENABLED", "").lower() in {"1", "true", "yes"}
SERVE_FRONTEND = os.getenv("SERVE_FRONTEND", "true").lower() not in {"0", "false", "no"}
EAGLEGIS_CSV_URL = os.getenv(
    "EAGLEGIS_CSV_URL",
    "https://raw.githubusercontent.com/EagleGIS-FGCU/EagleGIS/main/app/data/gold/meetings_ai_public.csv",
)
