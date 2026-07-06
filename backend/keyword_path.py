"""Keyword and application-ID lookup without LLM."""
from __future__ import annotations

import re

import pandas as pd

from models import ChatResponse, RouteKind
from router import APP_ID_RE, YEAR_RE
from structured_path import _row_to_project


def answer_keyword(df: pd.DataFrame, question: str) -> ChatResponse:
    out = df.copy()
    app_m = APP_ID_RE.search(question)
    if app_m and "ApplicationID" in out.columns:
        needle = app_m.group(1).upper()
        out = out[out["ApplicationID"].astype(str).str.upper().str.contains(needle, na=False)]
    else:
        year_m = YEAR_RE.search(question)
        if year_m and "MeetingYear" in out.columns:
            out = out[out["MeetingYear"].astype(str) == year_m.group(1)]
        tokens = [t for t in re.findall(r"[a-z0-9]{3,}", question.lower()) if t not in {
            "the", "and", "for", "what", "show", "minutes", "meeting", "estero",
        }]
        if tokens:
            mask = pd.Series(False, index=out.index)
            for col in ("ApplicationID", "ProjectName", "Location", "LocationName", "Filename"):
                if col in out.columns:
                    for tok in tokens:
                        mask |= out[col].astype(str).str.contains(tok, case=False, na=False)
            if mask.any():
                out = out[mask]

    n = len(out)
    if n == 0:
        return ChatResponse(
            summary="I don't have records on that.",
            projects=[],
            answer="I don't have records on that.",
            route=RouteKind.KEYWORD.value,
            meta={"matched_rows": 0},
        )
    projects = [_row_to_project(r) for r in out.head(8).to_dict(orient="records")]
    summary = f"Found {n} record{'s' if n != 1 else ''} matching your search."
    return ChatResponse(
        summary=summary,
        projects=projects,
        answer=summary,
        route=RouteKind.KEYWORD.value,
        meta={"matched_rows": n},
    )
