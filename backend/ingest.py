"""CSV -> retrieval chunks with rich, verbatim metadata.

Card-worthy fields (RecordId, PrimarySourceUrl, SourceFilename, article url, ...)
are copied straight into each chunk's metadata dict at index-build time so the
API can build cards from real data later instead of asking the LLM to invent them.
"""
from __future__ import annotations

import os
import re
from typing import Any

import pandas as pd
from langchain.schema import Document
from langchain.text_splitter import RecursiveCharacterTextSplitter

from schema_aliases import row_value

CHUNK_SIZE = 1000
CHUNK_OVERLAP = 150

_splitter = RecursiveCharacterTextSplitter(chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP)

_APPID_PAREN_RE = re.compile(r"\s*\((?:DOS|DCI|LDO|ADD|CPA|REZ)\d{4}[^)]*\)")
_TRAILING_DEV_ORDER_RE = re.compile(r"\s*(-\s*)?Development Order\s*$", re.IGNORECASE)

# Header lines we inject purely to help retrieval (DATE / SEARCH / SOURCE_TYPE).
# They must never be shown to a user — see strip_header_lines().
_HEADER_LINE_RE = re.compile(r"^(DATE|SEARCH|SOURCE_TYPE|TRUE_URL):.*$", re.IGNORECASE | re.MULTILINE)

# Best-effort section-heading detector for multi-project roundup articles, e.g.
# "Sandy Lane (Village of Estero) Bike-Ped Improvements – Village Districts 3 & 4".
# Falls back to treating the whole article as one section when it doesn't match
# (most single-topic articles), so this only changes behavior for the roundup style.
_SECTION_HEADING_RE = re.compile(
    r"[A-Z][^.!?]{2,110}?\s+[–—-]\s+(?:[A-Za-z0-9().;, ]{0,45}?)Districts?\s+[0-9][0-9,&;\s]{0,20}"
)


def clean_project_title(raw: str) -> str:
    """Trim a verbose ProjectName/ProjectTitle down to a short, human-readable title."""
    name = _APPID_PAREN_RE.split(str(raw), maxsplit=1)[0].strip()
    name = _TRAILING_DEV_ORDER_RE.sub("", name).strip()
    if len(name) > 120:
        name = name[:117].rsplit(" ", 1)[0].rstrip(".,;: ") + "…"
    return name or str(raw)[:120]


def strip_header_lines(text: str) -> str:
    """Remove injected DATE:/SEARCH:/SOURCE_TYPE: retrieval-aid lines before display."""
    cleaned = _HEADER_LINE_RE.sub("", text)
    cleaned = re.sub(r"\n{2,}", "\n", cleaned).strip()
    return cleaned


def _nonempty(v: Any) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    return "" if s.lower() in {"", "nan", "none"} else s


def _to_float(v: Any) -> float | None:
    s = _nonempty(v)
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _chunk_pieces(header: str, body: str) -> list[str]:
    """Split body text under header, keeping every piece under CHUNK_SIZE and
    never letting the recursive splitter cross a caller-defined section boundary."""
    if not body:
        return []
    if len(header) + len(body) + 2 <= CHUNK_SIZE:
        return [f"{header}\n\n{body}"]
    return [f"{header}\n\n{piece}" for piece in _splitter.split_text(body)]


# ─────────────────────────────────────────────
# Board records (data.csv — "meetings_ai_public" gold schema)
# ─────────────────────────────────────────────
def board_documents(df: pd.DataFrame) -> list[Document]:
    docs: list[Document] = []
    for _, row in df.iterrows():
        fields = {k: ("" if pd.isna(v) else v) for k, v in row.to_dict().items()}

        record_id = _nonempty(fields.get("RecordId"))
        if not record_id:
            # Never index a row we can't cite back to a real record — this is
            # exactly the class of row that used to let the LLM synthesize a
            # board card out of thin air.
            continue

        application_id = row_value(fields, "application_id")
        title = clean_project_title(row_value(fields, "project_name") or row_value(fields, "summary"))
        location = row_value(fields, "location")
        outcome = row_value(fields, "outcome", "action_taken", "status")
        meeting_date = row_value(fields, "meeting_date")
        primary_source_url = _nonempty(fields.get("PrimarySourceUrl"))
        source_filename = _nonempty(fields.get("SourceFilename"))
        summary = row_value(fields, "summary")
        lat = _to_float(row_value(fields, "latitude"))
        lng = _to_float(row_value(fields, "longitude"))

        metadata = {
            "source_type": "board_record",
            "record_id": record_id,
            "application_id": application_id,
            "project_name": title,
            "location": location,
            "outcome": outcome,
            "meeting_date": meeting_date,
            "date": meeting_date,
            "primary_source_url": primary_source_url,
            "source_filename": source_filename,
            "lat": lat,
            "lng": lng,
            "chunk_id": f"board-{record_id}",
        }

        date_header = f"DATE: {meeting_date}" if meeting_date else "DATE: unknown"
        search_header = "SEARCH: " + " | ".join(
            filter(None, [title, application_id, location, outcome[:60], meeting_date])
        )
        header = f"{date_header}\nSOURCE_TYPE: board_record\n{search_header}"

        body_lines = [
            f"ProjectName: {title}",
            f"ApplicationId: {application_id}",
            f"Location: {location}",
            f"MeetingDate: {meeting_date}",
            f"Outcome: {outcome}",
        ]
        if summary:
            body_lines.append(f"Summary: {summary}")
        body = "\n".join(line for line in body_lines if line.split(": ", 1)[-1])

        for piece in _chunk_pieces(header, body):
            docs.append(Document(page_content=piece, metadata=metadata))
    return docs


# ─────────────────────────────────────────────
# Articles (esterotoday_content.csv) — section-aware chunking
# ─────────────────────────────────────────────
def _split_sections(content: str) -> list[tuple[str, str]]:
    """Best-effort split of a multi-project roundup article into (heading, body)
    pairs so two different projects' details never share a chunk. Returns a
    single ("", content) section when fewer than 2 headings are found."""
    matches = list(_SECTION_HEADING_RE.finditer(content))
    if len(matches) < 2:
        return [("", content)]

    sections: list[tuple[str, str]] = []
    intro = content[: matches[0].start()].strip()
    if intro:
        sections.append(("", intro))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        heading = m.group(0).strip()
        body = content[m.end() : end].strip()
        if body:
            sections.append((heading, body))
    return sections


def article_documents(df: pd.DataFrame) -> list[Document]:
    docs: list[Document] = []
    for idx, row in df.iterrows():
        title = _nonempty(row.get("title"))
        url = _nonempty(row.get("url"))
        if not url:
            continue  # no verifiable link — never card-worthy, skip indexing
        content = _nonempty(row.get("content"))
        if not content:
            continue
        category = _nonempty(row.get("category"))
        publish_date = _nonempty(row.get("publish_date"))

        date_header = f"DATE: {publish_date}" if publish_date else "DATE: unknown"

        for sec_i, (heading, body) in enumerate(_split_sections(content)):
            metadata = {
                "source_type": "website_article",
                "title": title,
                "url": url,
                "category": category,
                "publish_date": publish_date,
                "date": publish_date,
                "section": heading,
                "chunk_id": f"article-{idx}-{sec_i}",
            }
            search_header = "SEARCH: " + " | ".join(filter(None, [title, heading[:80]]))
            header = f"{date_header}\nSOURCE_TYPE: website_article\n{search_header}"
            for piece in _chunk_pieces(header, body):
                docs.append(Document(page_content=piece, metadata=metadata))
    return docs


def build_documents(board_csv: str, website_csv: str) -> list[Document]:
    docs: list[Document] = []
    if os.path.exists(board_csv):
        board_df = pd.read_csv(board_csv, encoding="utf-8")
        board_docs = board_documents(board_df)
        docs.extend(board_docs)
        print(f"  {len(board_docs)} board chunks from {len(board_df)} rows")
    else:
        print(f"  Board CSV not found: {board_csv}")

    if os.path.exists(website_csv):
        website_df = pd.read_csv(website_csv, encoding="utf-8")
        article_docs = article_documents(website_df)
        docs.extend(article_docs)
        print(f"  {len(article_docs)} article chunks from {len(website_df)} rows")
    else:
        print(f"  Website CSV not found: {website_csv} (skipping)")

    return docs
