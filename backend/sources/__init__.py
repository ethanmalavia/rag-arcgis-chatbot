"""Registry of Engage Estero content sources.

Adding a source means appending one `SourceSpec` here plus a fetch function in
`wordpress.py`. The sync CLI, index wiring, and cache invalidation all iterate
this registry, so nothing else needs to learn about the new source.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

from config import (
    ENABLED_SOURCE_KEYS,
    ENGAGE_ESTERO_DIR,
    EXCLUDED_CATEGORY_SLUGS,
    LEGACY_ARTICLES_CSV,
)
from sources.base import (
    ContentRecord,
    SourceSpec,
    file_hash,
    has_excluded_category,
    read_records,
)

if TYPE_CHECKING:  # LangChain is only needed to build documents, not to sync.
    from langchain.schema import Document

SOURCE_SPECS: tuple[SourceSpec, ...] = (
    SourceSpec(
        key="posts",
        source_type="website_article",
        label="EsteroToday news posts",
        csv_name="posts.csv",
        legacy_csv=LEGACY_ARTICLES_CSV,
    ),
    SourceSpec(
        key="pages",
        source_type="website_page",
        label="Evergreen site pages",
        csv_name="pages.csv",
    ),
    SourceSpec(
        key="events",
        source_type="event",
        label="Community and Village events",
        csv_name="events.csv",
        # Upcoming events are answered live by events_path.answer_upcoming_events
        # via /api/events. Baking them into a weekly-rebuilt index would let the
        # bot cite events that have already happened, so sync but do not index.
        index_by_default=False,
    ),
    SourceSpec(
        key="documents",
        source_type="document",
        label="Published PDF documents",
        csv_name="documents.csv",
    ),
)

SPECS_BY_KEY = {spec.key: spec for spec in SOURCE_SPECS}


def enabled_specs() -> list[SourceSpec]:
    """Specs to index. ENABLED_SOURCE_KEYS is an explicit override."""
    if ENABLED_SOURCE_KEYS:
        return [spec for spec in SOURCE_SPECS if spec.key in ENABLED_SOURCE_KEYS]
    return [spec for spec in SOURCE_SPECS if spec.index_by_default]


def resolve_csv(spec: SourceSpec, base_dir: str = ENGAGE_ESTERO_DIR) -> str | None:
    """Preferred CSV for a source, falling back to its legacy file."""
    path = spec.csv_path(base_dir)
    if os.path.exists(path):
        return path
    if spec.legacy_csv and os.path.exists(spec.legacy_csv):
        return spec.legacy_csv
    return None


def load_records(spec: SourceSpec, base_dir: str = ENGAGE_ESTERO_DIR) -> list[ContentRecord]:
    path = resolve_csv(spec, base_dir)
    if not path:
        return []
    records = read_records(path)
    for record in records:
        # Legacy article rows predate record_id/source_type; backfill so they
        # merge and chunk identically to freshly synced rows.
        if not record.record_id:
            record.record_id = record.url or record.title
        if not record.source_type:
            record.source_type = spec.source_type
    # Second line of defense behind the fetch-time filter: a row that was
    # already committed, or later recategorized upstream, still never loads.
    kept = [r for r in records if not has_excluded_category(r.category, EXCLUDED_CATEGORY_SLUGS)]
    dropped = len(records) - len(kept)
    if dropped:
        print(f"  {spec.label}: dropped {dropped} row(s) in excluded categories")
    return kept


def source_hashes(base_dir: str = ENGAGE_ESTERO_DIR) -> dict[str, str]:
    """Content hash per enabled source, for index cache invalidation."""
    hashes: dict[str, str] = {}
    for spec in enabled_specs():
        path = resolve_csv(spec, base_dir)
        digest = file_hash(path) if path else None
        if digest:
            hashes[spec.key] = digest
    return hashes


def load_documents(base_dir: str = ENGAGE_ESTERO_DIR) -> list["Document"]:
    """Every enabled source's records as retrievable chunks.

    Imports the chunker here so the sync CLI can run with only `requests`
    installed, without pulling the LangChain stack.
    """
    from sources.documents import records_to_documents

    docs: list["Document"] = []
    for spec in enabled_specs():
        records = load_records(spec, base_dir)
        if not records:
            continue
        source_docs = records_to_documents(records)
        docs.extend(source_docs)
        print(f"  {spec.label}: {len(records)} records -> {len(source_docs)} chunks")
    return docs
