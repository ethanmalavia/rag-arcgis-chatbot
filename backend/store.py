"""Build/load FAISS + BM25 corpus and keep a pandas view for structured queries."""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
from langchain_core.documents import Document
from langchain_community.vectorstores import FAISS
from langchain_huggingface import HuggingFaceEmbeddings
from rank_bm25 import BM25Okapi

from chunking import rows_to_chunks
from schema_aliases import load_dataframe
from config import (
    BM25_FILE,
    CHUNK_SUMMARY_MIN,
    DEFAULT_CSV_PATH,
    EMBEDDING_MODEL,
    ENABLE_SUPPLEMENTAL_SOURCES,
    INDEX_DIR,
    MANIFEST_FILE,
)


def csv_hash(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


_DATE_IN_CHUNK = re.compile(r"meeting_date:\s*(\d{4}-\d{2}-\d{2})", re.IGNORECASE)
_YEAR_IN_CHUNK = re.compile(r"meeting_year:\s*(20\d{2})", re.IGNORECASE)


def _metadata_from_chunk(chunk_id: str, text: str) -> dict[str, Any]:
    """Recover date fields when reloading a BM25 corpus that only stored text."""
    meta: dict[str, Any] = {"chunk_id": chunk_id}
    dm = _DATE_IN_CHUNK.search(text or "")
    if dm:
        meta["meeting_date"] = dm.group(1)
        meta["meeting_year"] = dm.group(1)[:4]
    else:
        ym = _YEAR_IN_CHUNK.search(text or "")
        if ym:
            meta["meeting_year"] = ym.group(1)
    return meta


def _tokenize(text: str) -> list[str]:
    """BM25 tokens: drop short/stop words and light-stem simple plurals."""
    stop = {
        "are",
        "is",
        "was",
        "were",
        "there",
        "any",
        "the",
        "and",
        "for",
        "with",
        "from",
        "that",
        "this",
        "these",
        "those",
        "have",
        "has",
        "had",
        "did",
        "does",
        "do",
        "a",
        "an",
        "of",
        "to",
        "in",
        "on",
        "at",
        "by",
        "or",
        "as",
        "be",
        "it",
    }
    out: list[str] = []
    for tok in re.findall(r"[a-z0-9]+", text.lower()):
        if len(tok) < 3 or tok in stop:
            continue
        out.append(tok)
        if len(tok) > 4 and tok.endswith("s") and not tok.endswith("ss"):
            stem = tok[:-1]
            if stem not in stop and len(stem) >= 3:
                out.append(stem)
    return out


@dataclass
class DataStore:
    csv_path: str = DEFAULT_CSV_PATH
    dataframe: pd.DataFrame = field(default_factory=pd.DataFrame)
    documents: list[Document] = field(default_factory=list)
    vectorstore: FAISS | None = None
    bm25: BM25Okapi | None = None
    bm25_ids: list[str] = field(default_factory=list)
    record_count: int = 0
    chunk_count: int = 0
    embeddings: HuggingFaceEmbeddings | None = None
    _doc_by_id: dict[str, Document] | None = field(default=None, repr=False)
    # rag_path.retrieve_with_crag results for repeated questions. Lives on the
    # instance so a rebuilt index (a new DataStore) never serves stale hits.
    retrieval_cache: OrderedDict = field(default_factory=OrderedDict, repr=False)

    def is_ready(self) -> bool:
        return self.vectorstore is not None and self.bm25 is not None and not self.dataframe.empty

    def doc_by_id(self) -> dict[str, Document]:
        if self._doc_by_id is None:
            self._doc_by_id = {
                d.metadata.get("chunk_id", str(i)): d for i, d in enumerate(self.documents)
            }
        return self._doc_by_id



_store: DataStore | None = None


def get_store() -> DataStore | None:
    return _store


def get_embeddings() -> HuggingFaceEmbeddings:
    global _store
    if _store is not None and _store.embeddings is not None:
        return _store.embeddings
    print(f"Loading embedding model {EMBEDDING_MODEL}…")
    emb = HuggingFaceEmbeddings(
        model_name=EMBEDDING_MODEL,
        model_kwargs={"device": "cpu"},
        encode_kwargs={"batch_size": 64, "normalize_embeddings": True},
    )
    if _store is not None:
        _store.embeddings = emb
    print("Embedding model ready.")
    return emb


def _load_manifest() -> dict[str, Any]:
    if not os.path.exists(MANIFEST_FILE):
        return {}
    with open(MANIFEST_FILE, encoding="utf-8") as f:
        return json.load(f)


def _save_manifest(manifest: dict[str, Any]) -> None:
    os.makedirs(INDEX_DIR, exist_ok=True)
    with open(MANIFEST_FILE, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)


def _save_bm25(ids: list[str], corpus: list[str], metas: list[dict[str, Any]]) -> None:
    os.makedirs(INDEX_DIR, exist_ok=True)
    with open(BM25_FILE, "w", encoding="utf-8") as f:
        json.dump({"ids": ids, "corpus": corpus, "metas": metas}, f)


def _load_bm25() -> tuple[list[str], list[str], list[dict[str, Any]] | None]:
    with open(BM25_FILE, encoding="utf-8") as f:
        payload = json.load(f)
    return payload["ids"], payload["corpus"], payload.get("metas")


def _supplemental_documents() -> tuple[list[Document], dict[str, str]]:
    """Non-meeting Engage Estero content, plus per-source hashes for the manifest.

    Imported lazily so a missing/broken source never blocks the meetings index.
    """
    if not ENABLE_SUPPLEMENTAL_SOURCES:
        return [], {}
    try:
        from sources import load_documents, source_hashes
    except ImportError as exc:
        print(f"Supplemental sources unavailable ({exc}) — indexing meetings only")
        return [], {}
    try:
        hashes = source_hashes()
        if not hashes:
            return [], {}
        print("Loading supplemental Engage Estero sources…")
        return load_documents(), hashes
    except Exception as exc:
        print(f"Supplemental source load failed ({exc}) — indexing meetings only")
        return [], {}


def build_store(csv_path: str = DEFAULT_CSV_PATH) -> DataStore:
    """Build or load hybrid index for *csv_path* plus supplemental sources."""
    global _store
    digest = csv_hash(csv_path)
    supplemental_docs, supplemental_hashes = _supplemental_documents()
    manifest = _load_manifest()
    cache_ok = (
        manifest.get("csv_hash") == digest
        and manifest.get("embedding_model") == EMBEDDING_MODEL
        # Rebuild when any supplemental source changed, otherwise a fresh
        # content sync would never reach the index.
        and manifest.get("source_hashes", {}) == supplemental_hashes
        and os.path.exists(INDEX_DIR)
        and os.path.exists(BM25_FILE)
    )

    emb = get_embeddings()
    df = load_dataframe(csv_path)
    store = DataStore(csv_path=csv_path, dataframe=df, record_count=len(df), embeddings=emb)

    if cache_ok:
        print(f"Cache hit — loading index for {csv_path}")
        store.vectorstore = FAISS.load_local(INDEX_DIR, emb, allow_dangerous_deserialization=True)
        ids, corpus, metas = _load_bm25()
        store.bm25_ids = ids
        store.bm25 = BM25Okapi([_tokenize(t) for t in corpus])
        store.documents = [
            Document(
                page_content=text,
                # Caches written before metadata was persisted fall back to
                # recovering dates from the chunk text.
                metadata=(metas[i] if metas and i < len(metas) else _metadata_from_chunk(cid, text)),
            )
            for i, (cid, text) in enumerate(zip(ids, corpus))
        ]
        store.chunk_count = manifest.get("chunk_count", len(store.documents))
        _store = store
        print(f"Index loaded: {store.chunk_count} chunks, {store.record_count} rows")
        return store

    print(f"Building index for {csv_path}…")
    docs = rows_to_chunks(df, summary_min_len=CHUNK_SUMMARY_MIN)
    if supplemental_docs:
        print(f"Adding {len(supplemental_docs)} supplemental chunk(s) to the index")
        docs = docs + supplemental_docs
    store.documents = docs
    store.chunk_count = len(docs)
    store.vectorstore = FAISS.from_documents(docs, emb)
    store.vectorstore.save_local(INDEX_DIR)

    corpus_texts = [d.page_content for d in docs]
    store.bm25_ids = [d.metadata.get("chunk_id", str(i)) for i, d in enumerate(docs)]
    store.bm25 = BM25Okapi([_tokenize(t) for t in corpus_texts])
    _save_bm25(store.bm25_ids, corpus_texts, [d.metadata for d in docs])

    _save_manifest({
        "csv_hash": digest,
        "embedding_model": EMBEDDING_MODEL,
        "chunk_count": store.chunk_count,
        "record_count": store.record_count,
        "source_hashes": supplemental_hashes,
    })
    _store = store
    print(f"Index built: {store.chunk_count} chunks, {store.record_count} rows")
    return store
