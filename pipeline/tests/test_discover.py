"""Tests for the estero-fl.gov minutes discovery (no network)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from discover import (  # noqa: E402
    canonical_by_date,
    extract_pdf_urls,
    iso_date_from_filename,
    load_known,
)

BASE = "https://estero-fl.gov/wp-content/uploads/library-ada/minutes"


def test_date_from_mmddyyyy_filename():
    assert iso_date_from_filename("12172025.pdf") == "2025-12-17"


def test_date_from_yyyymmdd_filename():
    assert iso_date_from_filename("20240514 PZDB Minutes.pdf") == "2024-05-14"


def test_date_from_month_name_filename():
    assert iso_date_from_filename("Minutes January 8, 2025.pdf") == "2025-01-08"


def test_cancellation_notices_are_skipped():
    assert iso_date_from_filename("Cancellation Notice 04082025.pdf") is None
    assert iso_date_from_filename("Rescheduled 04082025.pdf") is None


def test_invalid_date_rejected():
    assert iso_date_from_filename("99999999.pdf") is None


def test_extract_pdf_urls_dedupes():
    html = (
        f'<a href="{BASE}/2025%20Minutes/Council/12172025.pdf">a</a>'
        f'<a href="{BASE}/2025%20Minutes/Council/12172025.pdf">dup</a>'
        f'<a href="https://elsewhere.example/x.pdf">offsite</a>'
    )
    urls = extract_pdf_urls(html)
    assert len(urls) == 1
    assert urls[0].endswith("12172025.pdf")


def test_canonical_prefers_approved_variant():
    urls = [
        f"{BASE}/2025/Council/12172025.pdf",
        f"{BASE}/2025/Council/12172025%20Approved.pdf",
    ]
    mapping, skipped = canonical_by_date(urls)
    assert mapping["2025-12-17"].endswith("Approved.pdf")
    assert skipped == []


def test_load_known_indexes_files_and_urls(tmp_path):
    pdf_dir = tmp_path / "pdfs"
    pdf_dir.mkdir()
    (pdf_dir / "12172025.pdf").write_bytes(b"%PDF")
    urls_file = tmp_path / "urls.txt"
    urls_file.write_text(f"{BASE}/2025%20Minutes/Council/01152025.pdf\n", encoding="utf-8")

    known_files, known_urls = load_known(pdf_dir, urls_file)
    assert "12172025.pdf" in known_files
    assert "01152025.pdf" in known_files  # from the URL line, unquoted
    assert len(known_urls) == 1
