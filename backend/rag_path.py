"""Corrective RAG path: hybrid retrieval, grading, rewrite, Groq generation."""
from __future__ import annotations

import json
import os
import re
from typing import Any

from langchain.prompts import PromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnablePassthrough
from langchain_groq import ChatGroq
from langchain.schema import Document

from config import CRAG_MAX_ITERS, GROQ_MODEL, SCORE_THRESHOLD
from models import ChatResponse, ProjectOut, RouteKind
from retrieval import best_score, format_docs, hybrid_retrieve
from store import DataStore

PROMPT_TEMPLATE = """You are a helpful assistant for the Village of Estero's Engage Estero platform.
You help residents understand Planning, Zoning & Design Board decisions using official meeting records.

RULES — follow exactly:
1. Only use facts from the Context. Never invent any detail.
2. If no relevant info exists, set summary to exactly: "I don't have records on that." and projects to [].
3. Write plain English in summary and project summaries. No markdown, no asterisks.
4. For each matching project, fill every field from the Context only.
5. document_url must be copied exactly from Document_Link in the context — never invent URLs.
6. status must be one of: Approved, Denied, Continued, or No decision recorded.

Return ONLY valid JSON (no markdown fences) with this exact shape:
{{
  "summary": "one closing sentence",
  "projects": [
    {{
      "title": "short project name from ProjectName",
      "id": "ApplicationID",
      "location": "Location or LocationName",
      "summary": "1-2 sentences",
      "status": "Approved | Denied | Continued | No decision recorded",
      "date": "MeetingDate",
      "document_url": "Document_Link"
    }}
  ]
}}

Context:
{context}

Question: {question}

JSON:"""

_llm: ChatGroq | None = None


def get_llm() -> ChatGroq:
    global _llm
    if _llm is None:
        _llm = ChatGroq(
            model=GROQ_MODEL,
            groq_api_key=os.getenv("GROQ_API_KEY"),
            temperature=0.0,
            max_tokens=1500,
        )
    return _llm


def _extract_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if match:
            return json.loads(match.group(0))
        raise


def parse_structured_answer(raw: str, route: str = RouteKind.RAG.value) -> ChatResponse:
    try:
        payload = _extract_json(raw)
        projects = [ProjectOut.model_validate(p) for p in payload.get("projects", [])]
        summary = str(payload.get("summary", "")).strip()
        if not summary and not projects:
            summary = "I don't have records on that."
        return ChatResponse(summary=summary, projects=projects, answer=summary, route=route)
    except Exception:
        return ChatResponse(summary=raw.strip(), projects=[], answer=raw.strip(), route=route)


def grade_context(hits: list[tuple[Document, float]]) -> str:
    if not hits:
        return "incorrect"
    score = best_score(hits)
    if score < SCORE_THRESHOLD * 0.5:
        return "incorrect"
    if score < SCORE_THRESHOLD:
        return "ambiguous"
    return "correct"


def rewrite_query(question: str) -> str:
    return f"{question.strip()} Estero Florida planning zoning design board"


def retrieve_with_crag(store: DataStore, question: str) -> tuple[str, dict[str, Any]]:
    query = question
    meta: dict[str, Any] = {"crag_iters": 0, "rewrites": []}
    hits: list[tuple[Document, float]] = []
    for i in range(CRAG_MAX_ITERS):
        meta["crag_iters"] = i + 1
        hits = hybrid_retrieve(store, query)
        verdict = grade_context(hits)
        meta["last_verdict"] = verdict
        if verdict == "correct":
            break
        if verdict in {"incorrect", "ambiguous"} and i < CRAG_MAX_ITERS - 1:
            query = rewrite_query(query)
            meta["rewrites"].append(query)
    meta["best_score"] = round(best_score(hits), 4)
    meta["retrieved"] = len(hits)
    return format_docs(hits), meta


def generate_answer(question: str, context: str, route: str = RouteKind.RAG.value) -> ChatResponse:
    prompt = PromptTemplate(template=PROMPT_TEMPLATE, input_variables=["context", "question"])
    chain = (
        {"context": lambda _: context, "question": RunnablePassthrough()}
        | prompt
        | get_llm()
        | StrOutputParser()
    )
    raw = chain.invoke(question)
    return parse_structured_answer(raw, route=route)


def answer_rag(store: DataStore, question: str) -> ChatResponse:
    context, crag_meta = retrieve_with_crag(store, question)
    result = generate_answer(question, context)
    result.route = RouteKind.RAG.value
    result.meta.update(crag_meta)
    return result


def stream_llm_tokens(question: str, context: str):
    """Yield text chunks from Groq for SSE streaming."""
    prompt = PromptTemplate(template=PROMPT_TEMPLATE, input_variables=["context", "question"])
    chain = (
        {"context": lambda _: context, "question": RunnablePassthrough()}
        | prompt
        | get_llm()
        | StrOutputParser()
    )
    for chunk in chain.stream(question):
        if chunk:
            yield chunk
