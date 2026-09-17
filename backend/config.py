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

# Non-meeting Engage Estero content (news posts, pages, events, PDF documents).
# One CSV per source under this directory; see docs/ARCHITECTURE_ALL_SOURCES.md.
ENGAGE_ESTERO_DIR = os.getenv("ENGAGE_ESTERO_DIR", os.path.join(DATA_DIR, "engage_estero"))
# Pre-registry article sync target, still read when posts.csv is absent.
LEGACY_ARTICLES_CSV = os.path.join(DATA_DIR, "esterotoday_content.csv")
# Index these sources alongside the gold meetings corpus.
ENABLE_SUPPLEMENTAL_SOURCES = os.getenv("ENABLE_SUPPLEMENTAL_SOURCES", "true").lower() not in {
    "0",
    "false",
    "no",
}
# Comma-separated source keys to index; empty means every registered source.
ENABLED_SOURCE_KEYS = {
    k.strip() for k in os.getenv("ENABLED_SOURCE_KEYS", "").split(",") if k.strip()
}
# WordPress category slugs whose posts must never reach the corpus. "limited"
# holds person profiles (board members, contributors) that should not surface as
# answers or results. Enforced when fetching and again when loading, so content
# added to an excluded category later is dropped without a re-scrape.
EXCLUDED_CATEGORY_SLUGS = {
    k.strip().lower()
    for k in os.getenv("EXCLUDED_CATEGORY_SLUGS", "limited").split(",")
    if k.strip()
}

EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
# MiniLM cross-encoder is ~5–10× faster on Cloud Run CPU than bge-reranker-base.
RERANKER_MODEL = os.getenv("RERANKER_MODEL", "cross-encoder/ms-marco-MiniLM-L-6-v2")
# Single Claude call extracts matching project facts and writes the citizen
# summary together (see rag_path.generate_answer).
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
# Skip LLM when keyword lookup returns a tight hit (app ID or ≤ N rows).
KEYWORD_FAST_MAX_ROWS = int(os.getenv("KEYWORD_FAST_MAX_ROWS", "6"))
ENABLE_KEYWORD_SHORTCUT = os.getenv("ENABLE_KEYWORD_SHORTCUT", "true").lower() not in {"0", "false", "no"}

DENSE_K = int(os.getenv("DENSE_K", "12"))
SPARSE_K = int(os.getenv("SPARSE_K", "12"))
RERANK_K = int(os.getenv("RERANK_K", "8"))
# How many fused hits to score with the cross-encoder (biggest CPU cost).
RERANK_CANDIDATES = int(os.getenv("RERANK_CANDIDATES", "12"))
SCORE_THRESHOLD = float(os.getenv("SCORE_THRESHOLD", "0.25"))
# One retrieve pass by default; set 2 to enable CRAG rewrite retry.
CRAG_MAX_ITERS = int(os.getenv("CRAG_MAX_ITERS", "1"))
CHUNK_SUMMARY_MIN = int(os.getenv("CHUNK_SUMMARY_MIN", "200"))
ENABLE_RERANKER = os.getenv("ENABLE_RERANKER", "true").lower() not in {"0", "false", "no"}
# Prefer newer meeting/article records in RAG ranking (0 disables).
ENABLE_RECENCY_BOOST = os.getenv("ENABLE_RECENCY_BOOST", "true").lower() not in {"0", "false", "no"}
# Default boost is strong enough that, with equal relevance, a newer article
# outranks an older one even when the user did not say "recent".
RECENCY_BOOST = float(os.getenv("RECENCY_BOOST", "0.55"))
# Days until a record's recency score halves (≈2 years — favors current coverage).
RECENCY_HALF_LIFE_DAYS = float(os.getenv("RECENCY_HALF_LIFE_DAYS", "730"))
# Conversational "recent/new/latest" queries: stronger boost + hard age window.
RECENT_QUERY_BOOST = float(os.getenv("RECENT_QUERY_BOOST", "1.5"))
RECENT_QUERY_MAX_AGE_YEARS = float(os.getenv("RECENT_QUERY_MAX_AGE_YEARS", "3"))
# Optional: Claude Haiku rewrites weak CRAG queries (falls back to rules if unset).
ENABLE_HAIKU_REWRITE = os.getenv("ENABLE_HAIKU_REWRITE", "true").lower() not in {"0", "false", "no"}
HAIKU_REWRITE_MODEL = os.getenv("HAIKU_REWRITE_MODEL", "claude-haiku-4-5-20251001")

# Project-scoped retrieval: when the top hits converge on one project (via the
# ProjectId grouping key from the gold corpus), expand to that project's full
# linked set (recall) and drop hits from a different project (precision).
ENABLE_PROJECT_SCOPE = os.getenv("ENABLE_PROJECT_SCOPE", "true").lower() not in {"0", "false", "no"}
PROJECT_SCOPE_MIN_SUPPORT = int(os.getenv("PROJECT_SCOPE_MIN_SUPPORT", "2"))
PROJECT_SCOPE_CAP = int(os.getenv("PROJECT_SCOPE_CAP", "20"))

# Warn users when an answer cites meeting records older than this many years.
STALE_SOURCE_YEARS = float(os.getenv("STALE_SOURCE_YEARS", "5"))
# Prompt pack under backend/prompts/<variant>/ (default | concise).
# concise = shorter resident answers (2–3 bullets, ≤3 project cards).
PROMPT_VARIANT = os.getenv("PROMPT_VARIANT", "concise").strip() or "concise"

FEEDBACK_DIR = os.path.join(DATA_DIR, "feedback")
FEEDBACK_FILE = os.path.join(FEEDBACK_DIR, "feedback.jsonl")
EVAL_REPORTS_DIR = os.path.join(DATA_DIR, "eval_reports")

OTEL_ENABLED = os.getenv("OTEL_ENABLED", "").lower() in {"1", "true", "yes"}
SERVE_FRONTEND = os.getenv("SERVE_FRONTEND", "true").lower() not in {"0", "false", "no"}
# Bearer token for /admin/* and /load. Leave empty to disable admin mutations.
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "").strip()
REPORTS_FILE = os.getenv(
    "REPORTS_FILE",
    os.path.join(DATA_DIR, "ops", "reports.json"),
)

# Public endpoint rate limits (in-memory, per process). Device = X-Device-Id.
ENABLE_RATE_LIMIT = os.getenv("ENABLE_RATE_LIMIT", "true").lower() not in {"0", "false", "no"}
RATE_LIMIT_CHAT_DEVICE = int(os.getenv("RATE_LIMIT_CHAT_DEVICE", "20"))
RATE_LIMIT_CHAT_IP = int(os.getenv("RATE_LIMIT_CHAT_IP", "40"))
RATE_LIMIT_CHAT_WINDOW_S = int(os.getenv("RATE_LIMIT_CHAT_WINDOW_S", "60"))
RATE_LIMIT_WRITE_DEVICE = int(os.getenv("RATE_LIMIT_WRITE_DEVICE", "10"))
RATE_LIMIT_WRITE_IP = int(os.getenv("RATE_LIMIT_WRITE_IP", "20"))
RATE_LIMIT_WRITE_WINDOW_S = int(os.getenv("RATE_LIMIT_WRITE_WINDOW_S", "60"))
