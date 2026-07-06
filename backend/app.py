"""FastAPI entrypoint: router-first Q&A API + optional static frontend."""
from __future__ import annotations

import logging
import os
import shutil
import traceback
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from config import DATA_DIR, DEFAULT_CSV_PATH, EMBEDDING_MODEL, FRONTEND_DIR, SERVE_FRONTEND
from models import ChatRequest, ChatResponse
from orchestrator import answer_question, stream_answer
from store import build_store, get_store

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(dotenv_path=os.path.join(BACKEND_DIR, ".env"))

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if os.path.exists(DEFAULT_CSV_PATH):
        build_store(DEFAULT_CSV_PATH)
    else:
        print(f"No CSV at {DEFAULT_CSV_PATH} — upload via /load or add data.csv")
    yield


app = FastAPI(title="Engage Estero RAG API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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
        raise HTTPException(500, str(e)) from e


@app.post("/chat/stream")
def chat_stream(req: ChatRequest):
    try:
        return StreamingResponse(stream_answer(req.question), media_type="text/event-stream")
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, str(e)) from e


@app.post("/load")
async def load_csv(file: UploadFile = File(...)):
    os.makedirs(DATA_DIR, exist_ok=True)
    dest = os.path.join(DATA_DIR, file.filename or "upload.csv")
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)
    try:
        build_store(dest)
        return {"message": f"Loaded {_record_count()} records from {file.filename}"}
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, str(e)) from e


if SERVE_FRONTEND and os.path.isdir(FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
