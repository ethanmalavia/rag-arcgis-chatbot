"""FastAPI entrypoint: router-first Q&A API + optional static frontend."""
from __future__ import annotations

import logging
import os
import re
import shutil
import traceback
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from admin_auth import require_admin
from config import (
    DATA_DIR,
    DEFAULT_CSV_PATH,
    EMBEDDING_MODEL,
    FRONTEND_DIR,
    RERANKER_MODEL,
    SERVE_FRONTEND,
)
import config as app_config
from models import (
    ChatRequest,
    ChatResponse,
    ReportCreate,
    ReportOut,
    ReportStatusUpdate,
)
from orchestrator import answer_question, stream_answer
from reports import create_report, list_reports, report_counts, update_report
from schema_aliases import row_value
from store import build_store, get_store

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(dotenv_path=os.path.join(BACKEND_DIR, ".env"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_SAFE_CSV_NAME = re.compile(r"^[\w.\- ]+\.csv$", re.IGNORECASE)


def _warm_models() -> dict[str, bool]:
    """Load reranker + LLMs and run one retrieve so the first user question is warm."""
    from rag_path import gemini_available, get_llm, groq_available
    from retrieval import get_reranker, hybrid_retrieve

    store = get_store()
    ready = store is not None and store.is_ready()
    reranker_ok = False
    llm_gemini_ok = False
    llm_groq_ok = False
    retrieve_ok = False
    if ready:
        get_reranker()
        reranker_ok = True
        hybrid_retrieve(store, "Estero planning zoning")
        retrieve_ok = True
        if gemini_available():
            try:
                get_llm("gemini")
                llm_gemini_ok = True
            except Exception as e:
                logger.warning("Gemini warmup skipped: %s", e)
        if groq_available():
            try:
                get_llm("groq")
                llm_groq_ok = True
            except Exception as e:
                logger.warning("Groq warmup skipped: %s", e)
    return {
        "chain_ready": ready,
        "reranker": reranker_ok,
        "llm": llm_gemini_ok or llm_groq_ok,
        "llm_gemini": llm_gemini_ok,
        "llm_groq": llm_groq_ok,
        "retrieve": retrieve_ok,
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    if os.path.exists(DEFAULT_CSV_PATH):
        build_store(DEFAULT_CSV_PATH)
        # Warm models in the background so /ready is not blocked for the startup probe.
        import threading

        def _bg_warm():
            try:
                status = _warm_models()
                logger.info("Startup warmup: %s", status)
            except Exception:
                traceback.print_exc()
                logger.warning("Startup warmup failed; first RAG request may be slow")

        threading.Thread(target=_bg_warm, daemon=True, name="model-warmup").start()
    else:
        print(f"No CSV at {DEFAULT_CSV_PATH} — run pipeline/build.py or upload via /load")
    yield


app = FastAPI(title="Engage Estero RAG API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class StaticCacheMiddleware(BaseHTTPMiddleware):
    """Cache fingerprinted assets only — never long-cache app.js/index (breaks Enter/send on deploy)."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path.startswith("/assets/"):
            response.headers["Cache-Control"] = "public, max-age=86400, stale-while-revalidate=604800"
        elif path.endswith((".js", ".css", ".html")) or path in {"", "/"}:
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


app.add_middleware(StaticCacheMiddleware)


def _record_count() -> int:
    store = get_store()
    return store.record_count if store else 0


def _chain_ready() -> bool:
    store = get_store()
    return store is not None and store.is_ready()


# Legacy exports for tests / Docker
build_or_load_index = build_store


@app.get("/health")
def health():
    store = get_store()
    return {
        "status": "ok",
        "chain_ready": _chain_ready(),
        "record_count": _record_count(),
        "chunk_count": store.chunk_count if store else 0,
        "embedding_model": EMBEDDING_MODEL,
    }


@app.get("/ready")
def ready():
    if not _chain_ready():
        raise HTTPException(503, "Index not loaded")
    return {"status": "ready", "record_count": _record_count()}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    try:
        return answer_question(req.question)
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, "Chat failed") from e


@app.get("/warmup")
def warmup():
    """Touch retrieval + LLM so the first user question is not a cold start."""
    try:
        status = _warm_models()
        if not status["chain_ready"]:
            raise HTTPException(503, "Index not loaded")
        if not status["reranker"]:
            raise HTTPException(503, "Reranker warmup failed")
        return {"status": "warm", **status}
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(503, "Warmup failed") from e


@app.post("/chat/stream")
def chat_stream(req: ChatRequest):
    try:
        return StreamingResponse(stream_answer(req.question), media_type="text/event-stream")
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, "Stream failed") from e


@app.post("/reports", response_model=ReportOut)
def submit_report(payload: ReportCreate):
    """Public: flag an incorrect location or suggest a data change."""
    try:
        return create_report(payload)
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, "Could not save report") from e


@app.get("/recent-decisions")
def recent_decisions(limit: int = 5):
    """Dashboard widget: recent board rows that have a project name."""
    store = get_store()
    if store is None or store.dataframe is None or store.dataframe.empty:
        raise HTTPException(503, "No board data loaded yet.")
    df = store.dataframe
    rows = df.to_dict(orient="records")

    def sort_key(row: dict) -> str:
        return row_value(row, "meeting_date") or ""

    named = [r for r in rows if row_value(r, "project_name")]
    named.sort(key=sort_key, reverse=True)
    decisions = []
    for row in named[: max(1, min(limit, 25))]:
        decisions.append(
            {
                "title": row_value(row, "project_name"),
                "date": row_value(row, "meeting_date") or None,
                "board": row_value(row, "board") or "Planning, Zoning & Design Board",
                "status": row_value(row, "status", "outcome", "action_taken") or None,
                "application_id": row_value(row, "application_id") or None,
            }
        )
    return {"decisions": decisions}


@app.get("/admin")
def admin_redirect():
    return RedirectResponse(url="/admin.html", status_code=307)


@app.get("/admin/status")
def admin_status(_: None = Depends(require_admin)):
    store = get_store()
    return {
        "status": "ok",
        "admin_configured": bool(app_config.ADMIN_API_KEY),
        "chain_ready": _chain_ready(),
        "record_count": _record_count(),
        "chunk_count": store.chunk_count if store else 0,
        "embedding_model": EMBEDDING_MODEL,
        "reranker_model": RERANKER_MODEL,
        "csv_path": DEFAULT_CSV_PATH,
        "reports": report_counts(),
    }


@app.get("/admin/reports", response_model=list[ReportOut])
def admin_list_reports(status: str | None = None, _: None = Depends(require_admin)):
    return list_reports(status=status)


@app.patch("/admin/reports/{report_id}", response_model=ReportOut)
def admin_update_report(
    report_id: str,
    payload: ReportStatusUpdate,
    _: None = Depends(require_admin),
):
    try:
        return update_report(report_id, payload)
    except KeyError:
        raise HTTPException(404, "Report not found") from None


@app.post("/load")
async def load_csv(file: UploadFile = File(...), _: None = Depends(require_admin)):
    """Replace the in-memory corpus (admin only). Prefer pipeline rebuild in production."""
    raw_name = os.path.basename(file.filename or "upload.csv")
    if not _SAFE_CSV_NAME.match(raw_name):
        raise HTTPException(400, "Filename must be a simple .csv name")
    os.makedirs(DATA_DIR, exist_ok=True)
    dest = os.path.join(DATA_DIR, raw_name)
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)
    try:
        build_store(dest)
        return {"message": f"Loaded {_record_count()} records from {raw_name}"}
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, "Failed to rebuild index from upload") from e


if SERVE_FRONTEND and os.path.isdir(FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
