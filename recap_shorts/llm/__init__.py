"""Use the user's signed-in Codex CLI for text and image understanding."""
from __future__ import annotations

import os


def make_script_llm(kind: str | None = None, model: str | None = None):
    kind = (kind or os.environ.get("RECAP_SCRIPT_LLM") or "codex").strip().lower()
    if kind != "codex":
        raise ValueError(f"대본 LLM은 codex만 지원합니다: {kind}. RECAP_SCRIPT_LLM=codex로 바꾸세요.")
    from recap_shorts import settings
    from .codex import CodexLLM

    selected = model or settings.model("script") or None
    return CodexLLM(selected)
