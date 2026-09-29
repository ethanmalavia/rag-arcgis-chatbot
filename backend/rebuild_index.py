"""Force a full rebuild of the FAISS/BM25 index, ignoring any existing cache.

store.py's build_store() already skips rebuilding when the CSV + supplemental
sources are unchanged (see its manifest.json cache-validity check) — this
script exists for the cases that check can't cover: forcing a rebuild after
an embedding-model change you don't want to wait for on next server start,
or recovering from a corrupted/stale index on disk.

Usage:
    backend/venv/Scripts/python.exe backend/rebuild_index.py
    backend/venv/Scripts/python.exe backend/rebuild_index.py --csv path/to/other.csv
"""
from __future__ import annotations

import argparse
import os

from config import DEFAULT_CSV_PATH, MANIFEST_FILE
from store import build_store


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", default=DEFAULT_CSV_PATH, help="CSV to index (default: %(default)s)")
    args = parser.parse_args()

    # Deleting just the manifest is enough: build_store()'s cache_ok check
    # requires manifest["csv_hash"] to match, so a missing manifest always
    # fails the check and forces the full rebuild-and-save path.
    if os.path.exists(MANIFEST_FILE):
        os.remove(MANIFEST_FILE)
        print(f"Removed {MANIFEST_FILE} — cache invalidated.")

    store = build_store(args.csv)
    print(f"Rebuilt: {store.chunk_count} chunks, {store.record_count} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
