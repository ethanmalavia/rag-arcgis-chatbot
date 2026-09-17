"""FastAPI entrypoint: router-first Q&A API + optional static frontend."""
from __future__ import annotations

import logging
import os
import re
import shutil
import traceback
from contextlib import AsyncExitStack, asynccontextmanager

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from admin_auth import require_admin
from agent_sync_mcp import (
    mcp as agent_sync_mcp,
    mcp_bearer_token,
    mcp_http_enabled,
    prepare_runtime_sync_dir,
)
from config import (
    DATA_DIR,
    DEFAULT_CSV_PATH,
    EMBEDDING_MODEL,
    FRONTEND_DIR,
    RERANKER_MODEL,
    SERVE_FRONTEND,
)
import config as app_config
from feedback_store import append_feedback
from models import (
    ChatRequest,
    ChatResponse,
    FeedbackRequest,
    ReportCreate,
    ReportOut,
    ReportStatusUpdate,
)
from events import router as events_router
from orchestrator import answer_question, stream_answer
from rate_limit import enforce_rate_limit
from reports import create_report, list_reports, report_counts, update_report
from schema_aliases import row_value
from store import build_store, get_store
BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(dotenv_path=os.path.join(BACKEND_DIR, ".env"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_SAFE_CSV_NAME = re.compile(r"^[\w.\- ]+\.csv$", re.IGNORECASE)


def _warm_models() -> dict[str, bool]:
    """Load reranker + LLM provider and run one retrieve so the first user question is warm."""
    from retrieval import get_reranker, hybrid_retrieve

    store = get_store()
    ready = store is not None and store.is_ready()
    reranker_ok = False
    llm_ok = False
    retrieve_ok = False
    if ready:
        get_reranker()
        reranker_ok = True
        hybrid_retrieve(store, "Estero planning zoning")
        retrieve_ok = True
        try:
            import llm_provider  # noqa: F401 — import validates the client/key

            llm_ok = True
        except Exception as e:
            logger.warning("LLM provider warmup skipped: %s", e)
    return {
        "chain_ready": ready,
        "reranker": reranker_ok,
        "llm": llm_ok,
        "retrieve": retrieve_ok,
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with AsyncExitStack() as stack:
        if mcp_http_enabled():
            prepare_runtime_sync_dir()
            # Required for Streamable HTTP session manager (Cloud Run / multi-instance).
            await stack.enter_async_context(agent_sync_mcp.session_manager.run())
            logger.info("engage-estero-sync MCP mounted (Streamable HTTP)")

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
app.include_router(events_router)


class McpAuthMiddleware(BaseHTTPMiddleware):
    """Require Bearer MCP_API_KEY (or ADMIN_API_KEY) for /mcp Streamable HTTP."""

    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/mcp" or request.url.path.startswith("/mcp/"):
            expected = mcp_bearer_token()
            if not expected:
                from fastapi.responses import JSONResponse

                return JSONResponse(
                    {"detail": "MCP not configured. Set MCP_API_KEY or ADMIN_API_KEY."},
                    status_code=503,
                )
            auth = request.headers.get("authorization") or ""
            if not auth.startswith("Bearer ") or auth[len("Bearer ") :].strip() != expected:
                from fastapi.responses import JSONResponse

                return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        return await call_next(request)


if mcp_http_enabled():
    app.add_middleware(McpAuthMiddleware)
    # streamable_http_path="/" → public URL is https://…/mcp
    app.mount("/mcp", agent_sync_mcp.streamable_http_app())


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
        "mcp_http": mcp_http_enabled(),
    }


@app.get("/ready")
def ready():
    if not _chain_ready():
        raise HTTPException(503, "Index not loaded")
    return {"status": "ready", "record_count": _record_count()}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, _: None = Depends(enforce_rate_limit("chat"))):
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
def chat_stream(req: ChatRequest, _: None = Depends(enforce_rate_limit("chat"))):
    try:
        return StreamingResponse(stream_answer(req.question), media_type="text/event-stream")
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, "Stream failed") from e


@app.post("/reports", response_model=ReportOut)
def submit_report(payload: ReportCreate, _: None = Depends(enforce_rate_limit("public_write"))):
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


@app.post("/feedback")
def feedback(req: FeedbackRequest, _: None = Depends(enforce_rate_limit("public_write"))):
    rating = (req.rating or "").strip().lower()
    if rating not in {"up", "down"}:
        raise HTTPException(400, "rating must be 'up' or 'down'")
    if not (req.question or "").strip():
        raise HTTPException(400, "question is required")
    try:
        req.rating = rating
        return append_feedback(req)
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, str(e)) from e


@app.post("/load")
async def load_csv(file: UploadFile = File(...), _: None = Depends(require_admin)):
    """Replace the in-memory corpus (admin only). Prefer pipeline rebuild in production."""
    raw_name = file.filename or "upload.csv"
    # Reject path components before basename so ../../evil.csv cannot sneak through.
    if raw_name != os.path.basename(raw_name) or not _SAFE_CSV_NAME.match(os.path.basename(raw_name)):
        raise HTTPException(400, "Filename must be a simple .csv name")
    raw_name = os.path.basename(raw_name)
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
