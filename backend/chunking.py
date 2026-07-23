"""Split CSV rows into retrieval-friendly chunks."""
from __future__ import annotations

import re
from typing import Any

import pandas as pd
from langchain.schema import Document

from schema_aliases import row_value


def _short_name(raw_name: str) -> str:
    name = re.split(r"\s*\((?:DOS|DCI|LDO|ADD|CPA|REZ)\d{4}", str(raw_name))[0].strip()
    return re.sub(r"\s*-\s*Development Order.*", "", name, flags=re.IGNORECASE).strip()[:80]


def build_search_header(fields: dict[str, Any]) -> str:
    raw_name = row_value(fields, "project_name")
    location = row_value(fields, "location")
    app_id = row_value(fields, "application_id")
    date = row_value(fields, "meeting_date")
    outcome = row_value(fields, "outcome", "action_taken", "status")
    header_parts = filter(None, [_short_name(raw_name), app_id, location, outcome[:60], date])
    return "SEARCH: " + " | ".join(header_parts)


def _format_fields(fields: dict[str, Any]) -> str:
    logical_keys = (
        "project_name", "project_id", "application_id", "location", "meeting_date",
        "meeting_year", "status", "summary", "action_taken", "outcome", "document_url",
    )
    lines = []
    for key in logical_keys:
        val = row_value(fields, key)
        if val:
            lines.append(f"{key}: {val}")
    return "\n".join(lines)


def rows_to_chunks(df: pd.DataFrame, summary_min_len: int = 200) -> list[Document]:
    chunks: list[Document] = []
    for idx, row in df.iterrows():
        fields = {k: ("" if pd.isna(v) else v) for k, v in row.to_dict().items()}
        app_id = row_value(fields, "application_id")
        base_meta = {
            "application_id": app_id,
            "row_index": int(idx),
            "meeting_year": row_value(fields, "meeting_year"),
            "meeting_date": row_value(fields, "meeting_date"),
            "project_id": row_value(fields, "project_id"),
            "project_key": row_value(fields, "project_key"),
        }
        header = build_search_header(fields)
        body = _format_fields(fields)
        chunks.append(
            Document(
                page_content=f"{header}\n\n{body}",
                metadata={**base_meta, "chunk_id": f"{idx}-meta", "chunk_type": "meta"},
            )
        )
        summary = row_value(fields, "summary")
        if len(summary) >= summary_min_len:
            chunks.append(
                Document(
                    page_content=f"{header}\n\nSummary: {summary}",
                    metadata={**base_meta, "chunk_id": f"{idx}-summary", "chunk_type": "summary"},
                )
            )
        action = row_value(fields, "action_taken", "outcome")
        if len(action) >= summary_min_len:
            chunks.append(
                Document(
                    page_content=f"{header}\n\nActionTaken: {action}",
                    metadata={**base_meta, "chunk_id": f"{idx}-action", "chunk_type": "action"},
                )
            )
    return chunks
