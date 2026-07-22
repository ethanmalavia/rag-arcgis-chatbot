"""Standalone retrieval diagnostic — loads the saved FAISS index directly
(no FastAPI, no app.py globals) and prints top-8 hits for a fixed set of
probe queries, in BOTH score spaces:

  RAW   = similarity_search_with_score()            (FAISS L2 distance, LOWER is better, unbounded)
  REL   = similarity_search_with_relevance_scores()  (normalized 0..1, HIGHER is better)

app.py's SCORE_THRESHOLD is compared against REL, not RAW — printing both
here so a raw distance is never accidentally compared against a relevance
threshold (that unit mismatch is an easy way to reintroduce this exact bug).

Usage:
    venv\\Scripts\\python.exe diagnose_retrieval.py
"""
from __future__ import annotations

from langchain_community.vectorstores import FAISS
from langchain_huggingface import HuggingFaceEmbeddings

INDEX_DIR = "faiss_index"
QUERIES = ["wawa", "Wawa development", "Goodwill", "Corkscrew Road"]

# Mirror app.py's embedding config exactly — a mismatch here would make this
# script's numbers meaningless for diagnosing app.py's behavior.
EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
ENCODE_KWARGS = {"batch_size": 64, "normalize_embeddings": True}

# Keep in sync with app.py's SCORE_THRESHOLD default — shown here only as a
# reference line, not enforced.
REFERENCE_THRESHOLD = 0.35


def label(doc) -> str:
    md = doc.metadata
    if md.get("source_type") == "board_record":
        return f"board  record_id={md.get('record_id')!r} project_name={md.get('project_name')!r}"
    return f"article url={md.get('url')!r} title={md.get('title')!r}"


def main() -> None:
    print(f"Loading embeddings ({EMBEDDING_MODEL})…")
    embeddings = HuggingFaceEmbeddings(model_name=EMBEDDING_MODEL, encode_kwargs=ENCODE_KWARGS)
    print(f"Loading FAISS index from {INDEX_DIR}/ …")
    vs = FAISS.load_local(INDEX_DIR, embeddings, allow_dangerous_deserialization=True)
    try:
        total = vs.index.ntotal
    except Exception:
        total = "?"
    print(f"Index loaded. Total vectors: {total}\n")

    for q in QUERIES:
        print("=" * 100)
        print(f"QUERY: {q!r}")
        print("=" * 100)

        print("\n-- RAW similarity_search_with_score (L2 distance, lower=better) --")
        raw_hits = vs.similarity_search_with_score(q, k=8)
        for doc, score in raw_hits:
            print(f"  raw={score:8.4f}   {label(doc)}")

        print(f"\n-- REL similarity_search_with_relevance_scores (0..1, higher=better; "
              f"app.py threshold={REFERENCE_THRESHOLD}) --")
        rel_hits = vs.similarity_search_with_relevance_scores(q, k=8)
        for doc, score in rel_hits:
            flag = " <-- PASSES threshold" if score >= REFERENCE_THRESHOLD else ""
            print(f"  rel={score:8.4f}   {label(doc)}{flag}")
        print()


if __name__ == "__main__":
    main()
