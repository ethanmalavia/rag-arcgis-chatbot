"""Persistent user reports (incorrect locations / suggested changes)."""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from config import REPORTS_FILE
from models import ReportCreate, ReportOut, ReportStatus, ReportStatusUpdate

_lock = threading.Lock()


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _ensure_parent() -> None:
    parent = os.path.dirname(REPORTS_FILE)
    if parent:
        os.makedirs(parent, exist_ok=True)


def _read_all() -> list[dict[str, Any]]:
    if not os.path.isfile(REPORTS_FILE):
        return []
    with open(REPORTS_FILE, encoding="utf-8") as f:
        raw = f.read().strip()
    if not raw:
        return []
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError("reports file must be a JSON array")
    return data


def _write_all(rows: list[dict[str, Any]]) -> None:
    _ensure_parent()
    tmp = REPORTS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, REPORTS_FILE)


def create_report(payload: ReportCreate) -> ReportOut:
    row = {
        "id": str(uuid.uuid4()),
        "created_at": _utc_now(),
        "kind": payload.kind.value,
        "status": ReportStatus.OPEN.value,
        "details": payload.details.strip(),
        "application_id": payload.application_id.strip(),
        "location": payload.location.strip(),
        "current_value": payload.current_value.strip(),
        "suggested_value": payload.suggested_value.strip(),
        "contact_email": payload.contact_email.strip(),
        "page_url": payload.page_url.strip(),
        "admin_note": "",
    }
    with _lock:
        rows = _read_all()
        rows.insert(0, row)
        _write_all(rows)
    return ReportOut.model_validate(row)


def list_reports(status: str | None = None) -> list[ReportOut]:
    with _lock:
        rows = _read_all()
    out = [ReportOut.model_validate(r) for r in rows]
    if status:
        out = [r for r in out if r.status.value == status]
    return out


def update_report(report_id: str, payload: ReportStatusUpdate) -> ReportOut:
    with _lock:
        rows = _read_all()
        for i, row in enumerate(rows):
            if row.get("id") == report_id:
                row["status"] = payload.status.value
                if payload.admin_note.strip():
                    row["admin_note"] = payload.admin_note.strip()
                rows[i] = row
                _write_all(rows)
                return ReportOut.model_validate(row)
    raise KeyError(report_id)


def report_counts() -> dict[str, int]:
    with _lock:
        rows = _read_all()
    counts = {s.value: 0 for s in ReportStatus}
    for row in rows:
        key = row.get("status", ReportStatus.OPEN.value)
        if key in counts:
            counts[key] += 1
        else:
            counts[key] = counts.get(key, 0) + 1
    counts["total"] = len(rows)
    return counts
