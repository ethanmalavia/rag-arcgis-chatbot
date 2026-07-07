"""Discover and download new meeting-minute PDFs from estero-fl.gov.

Adapted from the legacy EagleGIS repo's collect/minutes.py. Scrapes the
Village Council and PZDB minutes index pages, keeps one canonical PDF per
meeting date per body (preferring "approved"/"final"/"signed" variants),
and for every meeting not already in the corpus:

  - downloads the PDF into pdfs/
  - appends its URL to backend/data/bronze/estero_minutes_urls.txt

pipeline-refresh.yml runs this before rebuilding, which makes the weekly
refresh fully autonomous: new minutes posted on the village site flow into
the corpus without anyone committing a PDF by hand. Standalone dry run:

    python pipeline/discover.py --dry-run

Cancellation/reschedule notices without minutes are skipped (same as
legacy) — cancelled meetings still enter the corpus via their regular
minutes PDF, which the extractors flag as Cancelled.
"""
from __future__ import annotations

import argparse
import re
import ssl
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

SOURCES = {
    "council": "https://estero-fl.gov/villagecouncilminutes/",
    "pzdb": "https://estero-fl.gov/pzdbminutes/",
}

PDF_RE = re.compile(
    r'href="(https://estero-fl\.gov/wp-content/uploads/library-ada/minutes/[^"]+\.pdf[^"]*)"',
    re.IGNORECASE,
)

DATE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("mmddyyyy", re.compile(r"(?<!\d)(\d{2})(\d{2})(\d{4})(?!\d)")),
    ("iso", re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")),
    ("yyyymmdd", re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})(?!\d)")),
    ("mmddyy", re.compile(r"(?<!\d)(\d{2})(\d{2})(\d{2})(?!\d)")),
)

MONTH_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+(\d{1,2}),?\s+(\d{4})",
    re.IGNORECASE,
)
MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}

USER_AGENT = (
    "Mozilla/5.0 (compatible; EagleGIS-Minutes-Indexer/1.0; "
    "+https://github.com/krocks9903/rag-arcgis-chatbot)"
)

# estero-fl.gov presents a cert chain some runners can't verify; the verifier
# uses the same relaxation for Lee County.
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PDF_DIR = REPO_ROOT / "pdfs"
DEFAULT_URLS_FILE = REPO_ROOT / "backend" / "data" / "bronze" / "estero_minutes_urls.txt"


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60, context=SSL_CTX) as resp:
        return resp.read()


def fetch_text(url: str) -> str:
    return fetch(url).decode("utf-8", errors="replace")


def _safe_date(year: int, month: int, day: int) -> str | None:
    if not (2000 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31):
        return None
    try:
        return datetime(year, month, day).date().isoformat()
    except ValueError:
        return None


def iso_date_from_filename(filename: str) -> str | None:
    low = filename.lower()
    if low.startswith("cancel") or "cancellation" in low or "rescheduled" in low:
        return None
    m = MONTH_RE.search(filename)
    if m:
        month = MONTHS[m.group(1).lower()]
        return _safe_date(int(m.group(3)), month, int(m.group(2)))
    for kind, pat in DATE_PATTERNS:
        m = pat.search(filename)
        if not m:
            continue
        if kind == "mmddyyyy":
            mm, dd, yyyy = m.groups()
        elif kind in ("iso", "yyyymmdd"):
            yyyy, mm, dd = m.groups()
        else:  # mmddyy
            mm, dd, yy = m.groups()
            yyyy = str(2000 + int(yy) if int(yy) < 50 else 1900 + int(yy))
        iso = _safe_date(int(yyyy), int(mm), int(dd))
        if iso:
            return iso
    return None


def extract_pdf_urls(html: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for match in PDF_RE.finditer(html):
        url = match.group(1)
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _preference_score(name: str) -> int:
    low = name.lower()
    return sum(kw in low for kw in ("approved", "final", "signed"))


def canonical_by_date(pdf_urls: list[str]) -> tuple[dict[str, str], list[str]]:
    """Map ISO date -> canonical PDF URL; also return skipped filenames."""
    mapping: dict[str, str] = {}
    skipped: list[str] = []
    for url in pdf_urls:
        filename = urllib.parse.unquote(url.rsplit("/", 1)[-1])
        iso = iso_date_from_filename(filename)
        if not iso:
            skipped.append(filename)
            continue
        existing = mapping.get(iso)
        if existing is None or _preference_score(filename) > _preference_score(
            urllib.parse.unquote(existing.rsplit("/", 1)[-1])
        ):
            mapping[iso] = url
    return mapping, skipped


def _filename_key(name: str) -> str:
    return re.sub(r"\s+", " ", urllib.parse.unquote(name).lower().strip())


def load_known(pdf_dir: Path, urls_file: Path) -> tuple[set[str], set[str]]:
    """Existing corpus: normalized PDF filenames and already-listed URLs."""
    known_files = {
        _filename_key(p.name) for p in pdf_dir.glob("*.pdf")
    } if pdf_dir.exists() else set()
    known_urls: set[str] = set()
    if urls_file.exists():
        for line in urls_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                known_urls.add(line)
                known_files.add(_filename_key(line.rsplit("/", 1)[-1]))
    return known_files, known_urls


def discover(pdf_dir: Path, urls_file: Path, dry_run: bool = False) -> int:
    known_files, known_urls = load_known(pdf_dir, urls_file)
    new_entries: list[tuple[str, str, str]] = []  # (body, iso, url)

    for body, index_url in SOURCES.items():
        html = fetch_text(index_url)
        mapping, skipped = canonical_by_date(extract_pdf_urls(html))
        print(f"{body}: {len(mapping)} dated minutes on index page "
              f"({len(skipped)} undated/notice files ignored)")
        for iso, url in sorted(mapping.items()):
            filename = urllib.parse.unquote(url.rsplit("/", 1)[-1])
            if url in known_urls or _filename_key(filename) in known_files:
                continue
            new_entries.append((body, iso, url))

    if not new_entries:
        print("No new minutes found — corpus is up to date.")
        return 0

    for body, iso, url in new_entries:
        filename = urllib.parse.unquote(url.rsplit("/", 1)[-1])
        print(f"NEW {body} {iso}: {filename}")
        if dry_run:
            continue
        pdf_dir.mkdir(parents=True, exist_ok=True)
        (pdf_dir / filename).write_bytes(fetch(url))
        with urls_file.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(url + "\n")

    verb = "Would download" if dry_run else "Downloaded"
    print(f"{verb} {len(new_entries)} new PDF(s).")
    return len(new_entries)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf-dir", type=Path, default=DEFAULT_PDF_DIR)
    parser.add_argument("--urls-file", type=Path, default=DEFAULT_URLS_FILE)
    parser.add_argument("--dry-run", action="store_true",
                        help="Report new minutes without downloading anything")
    args = parser.parse_args()
    discover(args.pdf_dir, args.urls_file, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
