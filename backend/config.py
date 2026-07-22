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
GOLD_CSV_PATH = os.path.join(DATA_DIR, "gold", "meetings_ai_public.csv")
DEFAULT_CSV_PATH = os.getenv("CSV_PATH", GOLD_CSV_PATH)

EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
# MiniLM cross-encoder is ~5–10× faster on Cloud Run CPU than bge-reranker-base.
RERANKER_MODEL = os.getenv("RERANKER_MODEL", "cross-encoder/ms-marco-MiniLM-L-6-v2")
# Primary: Gemini extracts project facts. Groq writes the citizen summary (collaborate).
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")
ENABLE_LLM_COLLABORATE = os.getenv("ENABLE_LLM_COLLABORATE", "true").lower() not in {"0", "false", "no"}
# Skip LLM when keyword lookup returns a tight hit (app ID or ≤ N rows).
KEYWORD_FAST_MAX_ROWS = int(os.getenv("KEYWORD_FAST_MAX_ROWS", "6"))
ENABLE_KEYWORD_SHORTCUT = os.getenv("ENABLE_KEYWORD_SHORTCUT", "true").lower() not in {"0", "false", "no"}
# Legacy name; collaborate path supersedes Gemini→Groq escalate.
ENABLE_LLM_ESCALATE = os.getenv("ENABLE_LLM_ESCALATE", "true").lower() not in {"0", "false", "no"}

DENSE_K = int(os.getenv("DENSE_K", "12"))
SPARSE_K = int(os.getenv("SPARSE_K", "12"))
RERANK_K = int(os.getenv("RERANK_K", "5"))
# How many fused hits to score with the cross-encoder (biggest CPU cost).
RERANK_CANDIDATES = int(os.getenv("RERANK_CANDIDATES", "8"))
SCORE_THRESHOLD = float(os.getenv("SCORE_THRESHOLD", "0.25"))
# One retrieve pass by default; set 2 to enable CRAG rewrite retry.
CRAG_MAX_ITERS = int(os.getenv("CRAG_MAX_ITERS", "1"))
CHUNK_SUMMARY_MIN = int(os.getenv("CHUNK_SUMMARY_MIN", "200"))
ENABLE_RERANKER = os.getenv("ENABLE_RERANKER", "true").lower() not in {"0", "false", "no"}
# Prefer newer meeting records in RAG ranking (0 disables).
ENABLE_RECENCY_BOOST = os.getenv("ENABLE_RECENCY_BOOST", "true").lower() not in {"0", "false", "no"}
RECENCY_BOOST = float(os.getenv("RECENCY_BOOST", "0.35"))
# Days until a record's recency score halves (≈3 years).
RECENCY_HALF_LIFE_DAYS = float(os.getenv("RECENCY_HALF_LIFE_DAYS", "1095"))
# Warn users when an answer cites meeting records older than this many years.
STALE_SOURCE_YEARS = float(os.getenv("STALE_SOURCE_YEARS", "5"))

OTEL_ENABLED = os.getenv("OTEL_ENABLED", "").lower() in {"1", "true", "yes"}
SERVE_FRONTEND = os.getenv("SERVE_FRONTEND", "true").lower() not in {"0", "false", "no"}
# Bearer token for /admin/* and /load. Leave empty to disable admin mutations.
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "").strip()
REPORTS_FILE = os.getenv(
    "REPORTS_FILE",
    os.path.join(DATA_DIR, "ops", "reports.json"),
)
