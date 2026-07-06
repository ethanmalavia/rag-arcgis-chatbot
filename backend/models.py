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
    question: str
    session_id: str = "default"
