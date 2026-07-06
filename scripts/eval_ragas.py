#!/usr/bin/env python3
"""Optional RAGAS evaluation over tests/golden_qa.json (requires GROQ_API_KEY)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from config import DEFAULT_CSV_PATH  # noqa: E402
from orchestrator import answer_question  # noqa: E402
from store import build_store  # noqa: E402


def main() -> int:
    if not os.getenv("GROQ_API_KEY"):
        print("GROQ_API_KEY not set — skipping RAGAS eval")
        return 0

    try:
        from datasets import Dataset
        from ragas import evaluate
        from ragas.metrics import answer_relevancy, faithfulness
    except ImportError:
        print("Install requirements-eval.txt for RAGAS")
        return 1

    golden = json.loads((ROOT / "backend" / "tests" / "golden_qa.json").read_text())
    build_store(DEFAULT_CSV_PATH)

    questions = []
    answers = []
    contexts = []

    for case in golden:
        if case.get("route") not in {"rag", "mixed"}:
            continue
        result = answer_question(case["question"])
        questions.append(case["question"])
        answers.append(result.summary)
        contexts.append([p.summary for p in result.projects] or [result.summary])

    if not questions:
        print("No RAG cases to evaluate")
        return 0

    ds = Dataset.from_dict({"question": questions, "answer": answers, "contexts": contexts})
    scores = evaluate(ds, metrics=[faithfulness, answer_relevancy])
    print(scores)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
