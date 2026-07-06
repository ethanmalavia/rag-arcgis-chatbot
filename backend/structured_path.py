"""Deterministic pandas filters for counts and list-style questions."""
from __future__ import annotations

import re

import pandas as pd

from models import ChatResponse, ProjectOut, RouteKind

YEAR_RE = re.compile(r"\b(20\d{2})\b")
STATUS_MAP = {
    "approved": "Approved",
    "denied": "Denied",
    "continued": "Continued",
    "rezoning": "rezoning",
}


def _row_to_project(row: dict) -> ProjectOut:
    outcome = str(row.get("Outcome") or row.get("ActionTaken") or row.get("Status") or "")
    status = "No decision recorded"
    low = outcome.lower()
    if "approved" in low:
        status = "Approved"
    elif "denied" in low:
        status = "Denied"
    elif "continued" in low:
        status = "Continued"
    title = str(row.get("ProjectTitle") or row.get("ProjectName") or "")[:120]
    return ProjectOut(
        title=title,
        id=str(row.get("ApplicationID") or ""),
        location=str(row.get("Location") or row.get("LocationName") or ""),
        summary=str(row.get("Summary") or outcome)[:300],
        status=status,
        date=str(row.get("MeetingDate") or ""),
        document_url=str(row.get("Document_Link") or ""),
    )


def _apply_filters(df: pd.DataFrame, question: str) -> pd.DataFrame:
    out = df.copy()
    q = question.lower()
    year_m = YEAR_RE.search(question)
    if year_m and "MeetingYear" in out.columns:
        out = out[out["MeetingYear"].astype(str) == year_m.group(1)]
    if "approved" in q and "Outcome" in out.columns:
        out = out[out["Outcome"].astype(str).str.contains("Approved", case=False, na=False)]
    elif "denied" in q and "Outcome" in out.columns:
        out = out[out["Outcome"].astype(str).str.contains("Denied", case=False, na=False)]
    elif "continued" in q and "Outcome" in out.columns:
        out = out[out["Outcome"].astype(str).str.contains("Continued", case=False, na=False)]
    if "rezoning" in q:
        for col in ("ProjectName", "Summary", "Outcome"):
            if col in out.columns:
                out = out[out[col].astype(str).str.contains("rezon", case=False, na=False)]
                break
    # street / location token
    for token in re.findall(r"[a-z]{4,}", q):
        if token in {"how", "many", "what", "were", "was", "the", "show", "list", "estero"}:
            continue
        mask = pd.Series(False, index=out.index)
        for col in ("Location", "LocationName", "ProjectName", "Summary"):
            if col in out.columns:
                mask |= out[col].astype(str).str.contains(token, case=False, na=False)
        if mask.any():
            out = out[mask]
            break
    return out


def answer_structured(df: pd.DataFrame, question: str) -> ChatResponse:
    filtered = _apply_filters(df, question)
    n = len(filtered)
    if n == 0:
        return ChatResponse(
            summary="I don't have records on that.",
            projects=[],
            answer="I don't have records on that.",
            route=RouteKind.STRUCTURED.value,
            meta={"matched_rows": 0},
        )
    projects = [_row_to_project(r) for r in filtered.head(10).to_dict(orient="records")]
    summary = f"Found {n} matching record{'s' if n != 1 else ''} in the official dataset."
    return ChatResponse(
        summary=summary,
        projects=projects,
        answer=summary,
        route=RouteKind.STRUCTURED.value,
        meta={"matched_rows": n},
    )
