"""Load versioned prompt templates by PROMPT_VARIANT."""
from __future__ import annotations

import functools
from pathlib import Path

from config import BACKEND_DIR, PROMPT_VARIANT

PROMPTS_DIR = Path(BACKEND_DIR) / "prompts"
_VALID_NAMES = frozenset({"answer"})


@functools.lru_cache(maxsize=16)
def load_prompt(name: str, variant: str | None = None) -> str:
    """Return template text for a name under prompts/<variant>/."""
    if name not in _VALID_NAMES:
        raise ValueError(f"Unknown prompt name: {name}")
    chosen = (variant or PROMPT_VARIANT or "default").strip() or "default"
    path = PROMPTS_DIR / chosen / f"{name}.txt"
    if not path.is_file():
        fallback = PROMPTS_DIR / "default" / f"{name}.txt"
        if not fallback.is_file():
            raise FileNotFoundError(f"Missing prompt template: {path}")
        path = fallback
    return path.read_text(encoding="utf-8")


def clear_prompt_cache() -> None:
    load_prompt.cache_clear()
