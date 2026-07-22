"""API and pipeline data models."""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class RouteKind(str, Enum):
    STRUCTURED = "structured"
    KEYWORD = "keyword"
    RAG = "rag"
    MIXED = "mixed"


class ProjectOut(BaseModel):
    title: str = ""
    id: str = ""
    location: str = ""
    summary: str = ""
    status: str = "No decision recorded"
    date: str = ""
    document_url: str = ""


class ChatResponse(BaseModel):
    summary: str
    projects: list[ProjectOut] = Field(default_factory=list)
    answer: str = ""
    route: str = "rag"
    meta: dict[str, Any] = Field(default_factory=dict)


class ChatRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=4000)
    session_id: str = "default"


class ReportKind(str, Enum):
    INCORRECT_LOCATION = "incorrect_location"
    SUGGEST_CHANGE = "suggest_change"
    OTHER = "other"


class ReportStatus(str, Enum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class ReportCreate(BaseModel):
    kind: ReportKind
    details: str = Field(..., min_length=5, max_length=4000)
    application_id: str = Field(default="", max_length=120)
    location: str = Field(default="", max_length=500)
    current_value: str = Field(default="", max_length=1000)
    suggested_value: str = Field(default="", max_length=1000)
    contact_email: str = Field(default="", max_length=254)
    page_url: str = Field(default="", max_length=500)


class ReportOut(BaseModel):
    id: str
    created_at: str
    kind: ReportKind
    status: ReportStatus = ReportStatus.OPEN
    details: str
    application_id: str = ""
    location: str = ""
    current_value: str = ""
    suggested_value: str = ""
    contact_email: str = ""
    page_url: str = ""
    admin_note: str = ""


class ReportStatusUpdate(BaseModel):
    status: ReportStatus
    admin_note: str = Field(default="", max_length=2000)
