"""JSON parsing shared by the Codex content adapters."""
from __future__ import annotations

import json
import re


_ESCAPE = re.compile(r'\\(["\\/bfnrt]|u[0-9a-fA-F]{4})|\\')


def _first_object(text: str, strict: bool = True) -> dict | None:
    dec = json.JSONDecoder(strict=strict)
    for i in [i for i, ch in enumerate(text) if ch == "{"]:
        try:
            obj, _ = dec.raw_decode(text[i:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def parse_json_loose(text: str) -> dict:
    """모델 답에서 JSON 객체 하나를 꺼낸다 (```json 울타리, <think> 앞말, 앞뒤 글 허용).
    그대로 못 읽으면 한 번 더 너그럽게 읽는다 — 긴 글을 옮겨 적은 답에서 가끔 나오는 문자열 속 날 탭·줄바꿈과
    JSON 에 없는 이스케이프(\\' \\[ 같은 것, 백슬래시를 글자로 둔다)."""
    text = re.sub(r"<think>.*?(</think>|$)", "", text or "", flags=re.S)
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if m:
        text = m.group(1)
    obj = _first_object(text)
    if obj is None:
        fixed = _ESCAPE.sub(lambda m: m.group(0) if m.group(1) else "\\\\", text)
        obj = _first_object(fixed, strict=False)
    if obj is None:
        raise ValueError(f"no JSON object in model output: {text[:200]!r}")
    return obj
