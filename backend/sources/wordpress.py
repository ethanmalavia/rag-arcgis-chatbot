"""Fetch Engage Estero content from the esterotoday.com WordPress REST API.

The REST API is used instead of HTML scraping because it returns the site's own
content model — post types, taxonomies, dates, and canonical links — without
depending on theme markup. `/wp-json/wp/v2/types` is the authority on what
exists; the fetchers below cover every type that carries civic content.
"""
from __future__ import annotations

import html
import re
import time
from typing import Any, Callable

import requests

from config import EXCLUDED_CATEGORY_SLUGS
from sources.base import ContentRecord

SITE = "https://esterotoday.com"
WP_API = f"{SITE}/wp-json/wp/v2"
EVENTS_API = f"{SITE}/wp-json/tribe/events/v1/events"

USER_AGENT = (
    "EngageEsteroBot/1.0 "
    "(+https://github.com/noleysc/rag-arcgis-chatbot; "
    "weekly data sync for the Engage Estero chatbot)"
)
REQUEST_TIMEOUT = 45
REQUEST_DELAY_SECONDS = 0.4
# The posts endpoint 500s at per_page=100 once full content is included, so
# content-heavy collections page in smaller batches. _collect halves the batch
# and retries if a host still refuses.
PER_PAGE = 100
CONTENT_PER_PAGE = 25
MIN_PER_PAGE = 5

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _clean(raw: str | None) -> str:
    """WordPress returns rendered HTML; retrieval wants plain text."""
    if not raw:
        return ""
    text = _TAG_RE.sub(" ", raw)
    text = html.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def _rendered(node: Any) -> str:
    if isinstance(node, dict):
        return _clean(node.get("rendered"))
    return _clean(node if isinstance(node, str) else "")


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def _walk(
    session: requests.Session,
    url: str,
    params: dict[str, Any],
    limit: int | None,
    page_size: int,
) -> list[dict]:
    """One pass over a paginated REST collection."""
    out: list[dict] = []
    page = 1
    while True:
        query = dict(params, page=page, per_page=page_size)
        resp = session.get(url, params=query, timeout=REQUEST_TIMEOUT)
        # Past the last page, WP core answers 400 (rest_post_invalid_page_number)
        # while The Events Calendar answers 404. Either is the end of the walk —
        # but only after page 1, where they mean a bad endpoint instead.
        if resp.status_code in (400, 404) and page > 1:
            return out
        resp.raise_for_status()
        payload = resp.json()
        items = payload if isinstance(payload, list) else payload.get("events", [])
        if not items:
            return out
        out.extend(items)
        if limit is not None and len(out) >= limit:
            return out[:limit]
        total_pages = int(resp.headers.get("X-WP-TotalPages") or 0)
        if total_pages and page >= total_pages:
            return out
        page += 1
        time.sleep(REQUEST_DELAY_SECONDS)


def _collect(
    session: requests.Session,
    url: str,
    params: dict[str, Any],
    limit: int | None = None,
    page_size: int = PER_PAGE,
) -> list[dict]:
    """Walk a collection, halving the batch size if the host returns a 5xx.

    Restarting the walk is safe because these collections are small (hundreds of
    items) and results are deduplicated downstream by record_id.
    """
    size = page_size
    while True:
        try:
            return _walk(session, url, params, limit, size)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if status < 500 or size <= MIN_PER_PAGE:
                raise
            size = max(MIN_PER_PAGE, size // 2)
            print(f"  HTTP {status} — retrying with per_page={size}")


def _term_names(session: requests.Session, taxonomy: str) -> dict[int, str]:
    """Map term ids to names so records store readable categories."""
    names: dict[int, str] = {}
    try:
        for term in _collect(session, f"{WP_API}/{taxonomy}", {"_fields": "id,name"}):
            names[int(term["id"])] = _clean(term.get("name"))
    except requests.RequestException:
        pass
    return names


def _category_ids(session: requests.Session, slugs: set[str]) -> list[int]:
    """Resolve category slugs to ids so the API can filter server-side."""
    if not slugs:
        return []
    ids: list[int] = []
    try:
        for term in _collect(
            session, f"{WP_API}/categories", {"_fields": "id,slug", "slug": ",".join(sorted(slugs))}
        ):
            ids.append(int(term["id"]))
    except requests.RequestException as exc:
        # Not fatal: load_records filters by category name as a fallback.
        print(f"  could not resolve excluded categories ({exc})")
    return ids


def fetch_posts(session: requests.Session, limit: int | None = None) -> list[ContentRecord]:
    categories = _term_names(session, "categories")
    records = []
    params = {"_fields": "id,date,link,title,content,excerpt,categories", "status": "publish"}
    excluded_ids = _category_ids(session, EXCLUDED_CATEGORY_SLUGS)
    if excluded_ids:
        params["categories_exclude"] = ",".join(str(i) for i in excluded_ids)
        print(f"  excluding categories {sorted(EXCLUDED_CATEGORY_SLUGS)} (ids {excluded_ids})")
    for item in _collect(session, f"{WP_API}/posts", params, limit, CONTENT_PER_PAGE):
        cats = [categories.get(int(c), "") for c in (item.get("categories") or [])]
        records.append(
            ContentRecord(
                source_type="website_article",
                record_id=f"post-{item.get('id')}",
                title=_rendered(item.get("title")),
                category="; ".join(c for c in cats if c),
                publish_date=(item.get("date") or "")[:10],
                url=item.get("link") or "",
                content=_rendered(item.get("content")) or _rendered(item.get("excerpt")),
            )
        )
    return records


def fetch_pages(session: requests.Session, limit: int | None = None) -> list[ContentRecord]:
    records = []
    params = {"_fields": "id,date,modified,link,title,content", "status": "publish"}
    for item in _collect(session, f"{WP_API}/pages", params, limit, CONTENT_PER_PAGE):
        records.append(
            ContentRecord(
                source_type="website_page",
                record_id=f"page-{item.get('id')}",
                title=_rendered(item.get("title")),
                category="Site page",
                # Pages are evergreen; modified date reflects current guidance
                # better than original publish date for recency ranking.
                publish_date=(item.get("modified") or item.get("date") or "")[:10],
                url=item.get("link") or "",
                content=_rendered(item.get("content")),
            )
        )
    return records


def fetch_events(session: requests.Session, limit: int | None = None) -> list[ContentRecord]:
    records = []
    for item in _collect(session, EVENTS_API, {"status": "publish"}, limit, CONTENT_PER_PAGE):
        venue = item.get("venue") or {}
        cats = [_clean(c.get("name")) for c in (item.get("categories") or []) if isinstance(c, dict)]
        start = (item.get("start_date") or "")[:10]
        records.append(
            ContentRecord(
                source_type="event",
                record_id=f"event-{item.get('id')}",
                title=_clean(item.get("title")),
                category="; ".join(c for c in cats if c) or "Event",
                publish_date=start,
                url=item.get("url") or "",
                venue=_clean(venue.get("venue")) if isinstance(venue, dict) else "",
                location=_clean(venue.get("address")) if isinstance(venue, dict) else "",
                content=_clean(item.get("description")) or _clean(item.get("excerpt")),
            )
        )
    return records


def fetch_documents(
    session: requests.Session,
    limit: int | None = None,
    fetch_text: bool = False,
    max_pdf_mb: float = 8.0,
) -> list[ContentRecord]:
    """PDF attachments.

    Metadata-only by default: 719 PDFs make full extraction the expensive part of
    a sync, and title/caption/description already make a document findable and
    citable. Pass fetch_text to pull body text with PyMuPDF.
    """
    records = []
    params = {
        "_fields": "id,date,link,title,source_url,caption,description",
        "mime_type": "application/pdf",
    }
    for item in _collect(session, f"{WP_API}/media", params, limit):
        source_url = item.get("source_url") or ""
        title = _rendered(item.get("title"))
        parts = [_rendered(item.get("caption")), _rendered(item.get("description"))]
        content = " ".join(p for p in parts if p)
        if fetch_text and source_url:
            body = _pdf_text(session, source_url, max_pdf_mb)
            if body:
                content = f"{content} {body}".strip()
        records.append(
            ContentRecord(
                source_type="document",
                record_id=f"doc-{item.get('id')}",
                title=title,
                category="Published document",
                publish_date=(item.get("date") or "")[:10],
                url=item.get("link") or "",
                document_url=source_url,
                content=content or title,
            )
        )
    return records


def _pdf_text(session: requests.Session, url: str, max_pdf_mb: float) -> str:
    try:
        import fitz  # PyMuPDF, already a pipeline dependency
    except ImportError:
        print("  PyMuPDF not installed — skipping PDF text extraction")
        return ""
    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"  skip PDF ({exc}): {url}")
        return ""
    if len(resp.content) > max_pdf_mb * 1024 * 1024:
        print(f"  skip PDF (over {max_pdf_mb} MB): {url}")
        return ""
    try:
        with fitz.open(stream=resp.content, filetype="pdf") as doc:
            text = " ".join(page.get_text() for page in doc)
    except Exception as exc:  # corrupt or encrypted PDFs are common
        print(f"  skip PDF (parse failed: {exc}): {url}")
        return ""
    return _WS_RE.sub(" ", text).strip()


FETCHERS: dict[str, Callable[..., list[ContentRecord]]] = {
    "posts": fetch_posts,
    "pages": fetch_pages,
    "events": fetch_events,
    "documents": fetch_documents,
}


def fetch(key: str, limit: int | None = None, **kwargs: Any) -> list[ContentRecord]:
    fetcher = FETCHERS[key]
    with _session() as session:
        return fetcher(session, limit=limit, **kwargs)
