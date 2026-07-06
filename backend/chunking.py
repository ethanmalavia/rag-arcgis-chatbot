"""Split CSV rows into retrieval-friendly chunks."""
from __future__ import annotations

import re
from typing import Any

import pandas as pd
from langchain.schema import Document


def _short_name(raw_name: str) -> str:
    name = re.split(r"\s*\((?:DOS|DCI|LDO|ADD|CPA|REZ)\d{4}", str(raw_name))[0].strip()
    return re.sub(r"\s*-\s*Development Order.*", "", name, flags=re.IGNORECASE).strip()[:80]


def build_search_header(fields: dict[str, Any]) -> str:
    raw_name = str(fields.get("ProjectName", ""))
    location = str(fields.get("Location", "") or fields.get("LocationName", ""))
    app_id = str(fields.get("ApplicationID", ""))
    date = str(fields.get("MeetingDate", ""))
    outcome = str(fields.get("Outcome", "") or fields.get("ActionTaken", "") or fields.get("Status", ""))
    header_parts = filter(None, [_short_name(raw_name), app_id, location, outcome[:60], date])
    return "SEARCH: " + " | ".join(header_parts)


def _format_fields(fields: dict[str, Any]) -> str:
    keys = (
        "ProjectName", "ApplicationID", "Location", "LocationName", "MeetingDate",
        "MeetingYear", "Status", "Summary", "ActionTaken", "Outcome", "Document_Link",
    )
    lines = []
    for key in keys:
        val = fields.get(key)
        if val is not None and str(val).strip():
            lines.append(f"{key}: {val}")
    return "\n".join(lines)


def rows_to_chunks(df: pd.DataFrame, summary_min_len: int = 200) -> list[Document]:
    chunks: list[Document] = []
    for idx, row in df.iterrows():
        fields = {k: ("" if pd.isna(v) else v) for k, v in row.to_dict().items()}
        app_id = str(fields.get("ApplicationID", ""))
        base_meta = {
            "application_id": app_id,
            "row_index": int(idx),
            "meeting_year": str(fields.get("MeetingYear", "")),
        }
        header = build_search_header(fields)
        body = _format_fields(fields)
        chunks.append(
            Document(
                page_content=f"{header}\n\n{body}",
                metadata={**base_meta, "chunk_id": f"{idx}-meta", "chunk_type": "meta"},
            )
        )
        summary = str(fields.get("Summary", ""))
        if len(summary) >= summary_min_len:
            chunks.append(
                Document(
                    page_content=f"{header}\n\nSummary: {summary}",
                    metadata={**base_meta, "chunk_id": f"{idx}-summary", "chunk_type": "summary"},
                )
            )
        action = str(fields.get("ActionTaken", "") or fields.get("Outcome", ""))
        if len(action) >= summary_min_len:
            chunks.append(
                Document(
                    page_content=f"{header}\n\nActionTaken: {action}",
                    metadata={**base_meta, "chunk_id": f"{idx}-action", "chunk_type": "action"},
                )
            )
    return chunks
