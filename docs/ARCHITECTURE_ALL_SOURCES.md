# Architecture: using every Engage Estero content type

Status: **implemented (phase 1)** — connectors, sync CLI, and index wiring are in
the repo. Phase 2 (source-aware cards and prompt guidance) is scoped at the
bottom.

## Why this exists

The chatbot's live index was built from exactly one file:

```
backend/data/gold/meetings_ai_public.csv     # agenda items from minutes PDFs
```

Everything else Engage Estero publishes was invisible to retrieval. Notably
`backend/data/esterotoday_content.csv` already held 644 synced news articles that
were never indexed, because `store.py` builds chunks through `chunking.py` while
the multi-source `ingest.py` was never wired into `build_store()`.

## What Engage Estero actually publishes

`esterotoday.com` is the Engage Estero site ("EsteroToday.com by Engage Estero").
It is WordPress, so its REST API enumerates the full content model. Measured
counts:

| Content type | REST collection | Count | Value to a civic Q&A bot |
| --- | --- | --- | --- |
| Posts | `/wp/v2/posts` | 647 | News, council coverage, project updates |
| Media (PDF) | `/wp/v2/media?mime_type=application/pdf` | 719 | Quarterly reports, presentations, board packets |
| Pages | `/wp/v2/pages` | 61 | Answer Desk, advocacy positions, evergreen explainers |
| Events | `/tribe/events/v1/events` | 17 upcoming | Meetings, summits, luncheons |
| Categories | `/wp/v2/categories` | 113 | Facet/topic vocabulary |
| `project` CPT | `/wp/v2/project` | 0 | Registered but empty — ignored |

The `project` post type exists in the schema but has zero published entries, so
there is no tracker content to ingest there today.

## Design

### 1. One record shape for every source

All non-meeting content normalizes to a single flat row (`ContentRecord` in
`backend/sources/base.py`):

```
source_type, record_id, title, category, publish_date,
url, document_url, venue, location, content
```

Optional columns stay empty rather than branching the schema — an event fills
`venue`, a PDF fills `document_url`, an article fills neither. This keeps one CSV
reader, one chunker, and one metadata contract for every current and future
source.

### 2. Connector registry, not hardcoded scripts

Before this change each source was a bespoke script (`discover.py`,
`sync_esterotoday.py`) with no shared contract. Now every source is a
`SourceSpec` in one registry:

```python
# backend/sources/__init__.py
SOURCE_SPECS = (
    SourceSpec(key="posts",     source_type="website_article", ...),
    SourceSpec(key="pages",     source_type="website_page",    ...),
    SourceSpec(key="events",    source_type="event",           ...),
    SourceSpec(key="documents", source_type="document",        ...),
)
```

Adding a source means appending one `SourceSpec` and one fetch function. The sync
CLI, the index wiring, cache invalidation, and tests all pick it up automatically.

### 2b. Synced is not the same as indexed

Events are the exception to "index everything." `events_path.answer_upcoming_events`
answers event questions live from `/api/events`, which aggregates EsteroToday,
FGCU athletics, and a manual JSON fallback. Baking upcoming events into an index
that rebuilds weekly would let the bot cite events that already happened, so the
events spec sets `index_by_default=False`: the CSV is still synced for archival
and analysis, but retrieval leaves it alone. Override with
`ENABLED_SOURCE_KEYS=posts,pages,events,documents`.

### 3. Storage layout

```
backend/data/engage_estero/
├── posts.csv          # source_type=website_article
├── pages.csv          # source_type=website_page
├── events.csv         # source_type=event
└── documents.csv      # source_type=document
```

Committed to git like the existing gold CSVs, so VPS image builds are
reproducible and the index bakes into the image at Docker build time. The legacy
`backend/data/esterotoday_content.csv` is still read as a fallback for `posts`
when `posts.csv` is absent, so nothing breaks mid-migration.

### 4. Retrieval: supplemental docs merge into the same index

`build_store()` keeps the tuned gold-CSV path and appends supplemental documents:

```
gold CSV ──► chunking.rows_to_chunks ──┐
                                       ├──► FAISS + BM25 (one index)
engage_estero/*.csv ──► sources.load_documents ──┘
```

One index means hybrid retrieval, reranking, and recency ranking work across
sources with no routing changes: a question about the Bonita Estero Rail Trail
can surface a 2026 news post, a council motion, and a quarterly report PDF
together, ordered newest-first by the existing recency boost.

Every supplemental chunk carries `DATE:` and `SOURCE_TYPE:` header lines in its
text, so `retrieval.document_meeting_date()` resolves dates even when metadata is
lost, and the reranker sees the source kind.

### 5. Two correctness fixes this required

**Cache invalidation.** The index manifest keyed only on the gold CSV's hash, so
syncing new articles would not rebuild the index. The manifest now stores a
`source_hashes` map and rebuilds when any source file changes.

**Metadata survival.** On a cache hit, documents were reconstructed from the BM25
text sidecar and metadata was regenerated by regex — which recovered dates but
dropped everything else. For articles and PDFs that would silently discard
`url`/`document_url`, breaking citations. The sidecar now persists each chunk's
metadata dict, falling back to the old regex path for pre-existing caches.

### 6. Refresh schedule

`.github/workflows/sync-engage-estero.yml` runs Fridays at 19:30 UTC — after the
minutes pipeline (18:00) and the legacy article sync (19:00) — and commits any
changed CSVs, which triggers the VPS deploy that rebakes the index.

Syncs are incremental and append-only by `record_id`; existing rows are never
rewritten, so a WordPress edit does not churn the corpus.

## PDF handling

719 PDFs is the largest single content pool, and full text extraction is the
expensive part (download + parse + embed). The connector splits this:

- **Default:** index each PDF's title, caption, description, and URL. Documents
  become findable and citable at near-zero cost.
- **Opt-in (`--fetch-pdf-text`):** download and extract body text with PyMuPDF,
  capped by `--max-pdf-mb`. Run selectively for high-value document sets.

This keeps the default sync fast and the production image small while leaving a
one-flag path to deep document search.

## Phase 2 (not yet built)

1. **Source-aware cards.** `frontend-react/src/types.ts` currently unions
   `board_record | website_article | village_council`. Add `website_page`,
   `event`, `document` and route them in `Message.tsx` (an event card wants date
   and venue; a document card wants a download link).
2. **Prompt guidance.** `prompts/*/extract.txt` describes only board-record
   fields. Teach the extractor to emit `source_type` and the per-type fields so
   the LLM stops flattening a PDF into a fake project card.
3. **Category facets.** Use the 113 WordPress categories as retrieval filters for
   topic questions ("what has Engage Estero said about water quality?").
