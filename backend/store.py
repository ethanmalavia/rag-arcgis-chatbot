"""Build/load FAISS + BM25 corpus and keep a pandas view for structured queries."""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
from langchain.schema import Document
from langchain_community.vectorstores import FAISS
from langchain_huggingface import HuggingFaceEmbeddings
from rank_bm25 import BM25Okapi

from chunking import rows_to_chunks
from config import (
    BM25_FILE,
    CHUNK_SUMMARY_MIN,
    DEFAULT_CSV_PATH,
    EMBEDDING_MODEL,
    INDEX_DIR,
    MANIFEST_FILE,
)


def csv_hash(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


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

    def is_ready(self) -> bool:
        return self.vectorstore is not None and self.bm25 is not None and not self.dataframe.empty


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
        encode_kwargs={"batch_size": 32, "normalize_embeddings": True},
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


def _save_bm25(ids: list[str], corpus: list[str]) -> None:
    os.makedirs(INDEX_DIR, exist_ok=True)
    with open(BM25_FILE, "w", encoding="utf-8") as f:
        json.dump({"ids": ids, "corpus": corpus}, f)


def _load_bm25() -> tuple[list[str], list[str]]:
    with open(BM25_FILE, encoding="utf-8") as f:
        payload = json.load(f)
    return payload["ids"], payload["corpus"]


def build_store(csv_path: str = DEFAULT_CSV_PATH) -> DataStore:
    """Build or load hybrid index for *csv_path*."""
    global _store
    digest = csv_hash(csv_path)
    manifest = _load_manifest()
    cache_ok = (
        manifest.get("csv_hash") == digest
        and manifest.get("embedding_model") == EMBEDDING_MODEL
        and os.path.exists(INDEX_DIR)
        and os.path.exists(BM25_FILE)
    )

    emb = get_embeddings()
    df = pd.read_csv(csv_path, encoding="utf-8")
    store = DataStore(csv_path=csv_path, dataframe=df, record_count=len(df), embeddings=emb)

    if cache_ok:
        print(f"Cache hit — loading index for {csv_path}")
        store.vectorstore = FAISS.load_local(INDEX_DIR, emb, allow_dangerous_deserialization=True)
        ids, corpus = _load_bm25()
        store.bm25_ids = ids
        store.bm25 = BM25Okapi([_tokenize(t) for t in corpus])
        store.documents = [
            Document(page_content=text, metadata={"chunk_id": cid})
            for cid, text in zip(ids, corpus)
        ]
        store.chunk_count = manifest.get("chunk_count", len(store.documents))
        _store = store
        print(f"Index loaded: {store.chunk_count} chunks, {store.record_count} rows")
        return store

    print(f"Building index for {csv_path}…")
    docs = rows_to_chunks(df, summary_min_len=CHUNK_SUMMARY_MIN)
    store.documents = docs
    store.chunk_count = len(docs)
    store.vectorstore = FAISS.from_documents(docs, emb)
    store.vectorstore.save_local(INDEX_DIR)

    corpus_texts = [d.page_content for d in docs]
    store.bm25_ids = [d.metadata.get("chunk_id", str(i)) for i, d in enumerate(docs)]
    store.bm25 = BM25Okapi([_tokenize(t) for t in corpus_texts])
    _save_bm25(store.bm25_ids, corpus_texts)

    _save_manifest({
        "csv_hash": digest,
        "embedding_model": EMBEDDING_MODEL,
        "chunk_count": store.chunk_count,
        "record_count": store.record_count,
    })
    _store = store
    print(f"Index built: {store.chunk_count} chunks, {store.record_count} rows")
    return store
