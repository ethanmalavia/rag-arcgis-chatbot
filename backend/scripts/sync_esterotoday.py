"""Incrementally sync backend/data/esterotoday_content.csv from esterotoday.com.

Discovers article URLs from:
1. Yoast `sitemap_index.xml` → each `post-sitemap*.xml` (soft-fail per child —
   some shards intermittently 500)
2. WordPress REST `/wp-json/wp/v2/posts` (reliable for newest posts when the
   primary Yoast shards are down)

Scrapes any article URL not already present in the CSV and appends new rows.
Existing rows are never modified, re-scraped, or removed.

Per-article extraction uses the page's own Yoast SEO JSON-LD `Article` node
(headline / datePublished / articleSection) plus the `.entry-content` text.

Usage:
    python backend/scripts/sync_esterotoday.py
    python backend/scripts/sync_esterotoday.py --dry-run
    python backend/scripts/sync_esterotoday.py --limit 5

Run in CI: see .github/workflows/sync-esterotoday.yml
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

SITE = "https://esterotoday.com"
SITEMAP_INDEX_URL = f"{SITE}/sitemap_index.xml"
WP_POSTS_API = f"{SITE}/wp-json/wp/v2/posts"
CSV_PATH = Path(__file__).resolve().parent.parent / "data" / "esterotoday_content.csv"
CSV_FIELDS = ["source_type", "title", "category", "publish_date", "url", "content"]
USER_AGENT = (
    "EngageEsteroBot/1.0 "
    "(+https://github.com/krocks9903/rag-arcgis-chatbot; "
    "weekly data sync for the Engage Estero chatbot)"
)
REQUEST_DELAY_SECONDS = 0.75
REQUEST_TIMEOUT = 20

_LD_JSON_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL,
)
_SITEMAP_LOC_RE = re.compile(r"<loc>(.*?)</loc>")


def _get(url: str, **params) -> requests.Response:
    return requests.get(
        url,
        params=params or None,
        headers={"User-Agent": USER_AGENT},
        timeout=REQUEST_TIMEOUT,
    )


def fetch_sitemap_post_urls() -> list[str]:
    """Collect post URLs from Yoast post-sitemap shards listed in sitemap_index.

    Soft-fails individual shards (EsteroToday's post-sitemap1/2 often 500).
    """
    try:
        resp = _get(SITEMAP_INDEX_URL)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"  warn: sitemap_index failed: {exc}")
        return []

    child_sitemaps = [
        loc
        for loc in _SITEMAP_LOC_RE.findall(resp.text)
        if "/post-sitemap" in loc
    ]
    print(f"  sitemap_index lists {len(child_sitemaps)} post-sitemap shard(s)")

    urls: list[str] = []
    for sm_url in child_sitemaps:
        try:
            child = _get(sm_url)
            if not child.ok:
                print(f"  warn: skip {sm_url} (HTTP {child.status_code})")
                continue
            ctype = (child.headers.get("content-type") or "").lower()
            if "xml" not in ctype and "<urlset" not in child.text[:800]:
                print(f"  warn: skip {sm_url} (non-XML response)")
                continue
            found = _SITEMAP_LOC_RE.findall(child.text)
            print(f"  ok {sm_url} → {len(found)} URL(s)")
            urls.extend(found)
        except requests.RequestException as exc:
            print(f"  warn: skip {sm_url}: {exc}")
        time.sleep(0.25)
    return urls


def fetch_wp_rest_post_urls(*, max_pages: int = 20) -> list[str]:
    """Paginate WP REST posts (newest first). Covers shards Yoast fails on."""
    urls: list[str] = []
    page = 1
    total_pages = 1
    while page <= max_pages and page <= total_pages:
        try:
            resp = _get(
                WP_POSTS_API,
                per_page=100,
                page=page,
                orderby="date",
                order="desc",
                _fields="link",
            )
        except requests.RequestException as exc:
            print(f"  warn: WP REST page {page} failed: {exc}")
            break
        if resp.status_code in {400, 404}:
            break
        if not resp.ok:
            print(f"  warn: WP REST page {page} HTTP {resp.status_code}")
            break
        try:
            total_pages = int(resp.headers.get("X-WP-TotalPages") or page)
        except ValueError:
            total_pages = page
        batch = resp.json()
        if not isinstance(batch, list) or not batch:
            break
        page_urls = [p.get("link") for p in batch if isinstance(p, dict) and p.get("link")]
        urls.extend(page_urls)
        print(f"  ok WP REST page {page}/{total_pages} → {len(page_urls)} URL(s)")
        page += 1
        if page <= total_pages:
            time.sleep(REQUEST_DELAY_SECONDS)
    return urls


def fetch_post_urls() -> list[str]:
    """Union of sitemap + REST discovery, newest-first preference preserved."""
    print(f"Fetching sitemap index: {SITEMAP_INDEX_URL}")
    sitemap_urls = fetch_sitemap_post_urls()
    print(f"Fetching WP REST posts: {WP_POSTS_API}")
    rest_urls = fetch_wp_rest_post_urls()

    merged: list[str] = []
    seen: set[str] = set()
    # REST first so newest candidates appear at the top for --limit / scraping order.
    for url in rest_urls + sitemap_urls:
        if url and url not in seen:
            seen.add(url)
            merged.append(url)
    print(
        f"  discovery: sitemap={len(sitemap_urls)} rest={len(rest_urls)} "
        f"unique={len(merged)}"
    )
    return merged


def load_existing_rows() -> tuple[list[dict], set[str]]:
    if not CSV_PATH.exists():
        return [], set()
    with open(CSV_PATH, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    return rows, {r["url"] for r in rows}


def _extract_article_node(page_html: str) -> dict | None:
    """Find the Yoast-emitted `Article` node inside the page's @graph JSON-LD
    block(s). A page can have multiple ld+json scripts (events widgets, etc.)
    — only one carries @type == "Article"."""
    for block in _LD_JSON_RE.findall(page_html):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        graph = data.get("@graph", []) if isinstance(data, dict) else []
        for node in graph:
            if isinstance(node, dict) and node.get("@type") == "Article":
                return node
    return None


def scrape_article(url: str) -> dict | None:
    resp = _get(url)
    if not resp.ok:
        print(f"  skip (HTTP {resp.status_code}): {url}")
        return None
    resp.encoding = resp.apparent_encoding or resp.encoding
    page_html = resp.text

    article = _extract_article_node(page_html)
    if article is None:
        print(f"  skip (no Article schema found — probably not a news post): {url}")
        return None

    # Yoast's JSON-LD embeds these fields HTML-entity-encoded (e.g. "&#8217;"
    # for a right single quote) even though it's a JSON string value, not
    # HTML — unescape so the CSV stores the actual character, matching the
    # existing rows.
    title = html.unescape((article.get("headline") or "").strip())
    if not title:
        print(f"  skip (no headline): {url}")
        return None

    soup = BeautifulSoup(page_html, "html.parser")
    content_nodes = soup.select(".entry-content")
    if not content_nodes:
        print(f"  skip (no .entry-content found): {url}")
        return None
    content = content_nodes[0].get_text(" ", strip=True)
    if not content:
        print(f"  skip (empty content): {url}")
        return None

    sections = article.get("articleSection") or []
    if isinstance(sections, str):
        sections = [sections]
    category = "; ".join(html.unescape(s) for s in sections if s)

    published = article.get("datePublished") or ""
    publish_date = published[:10] if published else ""

    return {
        "source_type": "website_article",
        "title": title,
        "category": category,
        "publish_date": publish_date,
        "url": url,
        "content": content,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Scrape and report, but never write the CSV.")
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N new URLs (for testing).")
    args = parser.parse_args()

    discovered = fetch_post_urls()
    if not discovered:
        print("ERROR: no post URLs discovered from sitemap or WP REST.", file=sys.stderr)
        return 1
    print(f"  {len(discovered)} URLs discovered")

    existing_rows, known_urls = load_existing_rows()
    print(f"  {len(known_urls)} articles already in {CSV_PATH.name}")

    new_urls = [u for u in discovered if u not in known_urls]
    if args.limit is not None:
        new_urls = new_urls[: args.limit]
    print(f"  {len(new_urls)} candidate new URL(s) to scrape")

    if not new_urls:
        print("Nothing to do.")
        return 0

    new_rows: list[dict] = []
    for i, url in enumerate(new_urls, 1):
        print(f"[{i}/{len(new_urls)}] {url}")
        try:
            row = scrape_article(url)
        except requests.RequestException as exc:
            print(f"  skip (request failed): {exc}")
            row = None
        if row:
            new_rows.append(row)
        if i < len(new_urls):
            time.sleep(REQUEST_DELAY_SECONDS)

    print(f"Scraped {len(new_rows)}/{len(new_urls)} new article(s) successfully.")
    if not new_rows:
        return 0

    if args.dry_run:
        print("--dry-run set: not writing the CSV. Sample of what would be added:")
        for row in new_rows[:3]:
            print(f"  - {row['publish_date']} | {row['category']!r} | {row['title']}")
        return 0

    all_rows = existing_rows + new_rows
    with open(CSV_PATH, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(all_rows)
    print(f"Wrote {len(all_rows)} total rows to {CSV_PATH} ({len(new_rows)} new).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
