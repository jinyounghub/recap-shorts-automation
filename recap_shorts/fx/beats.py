"""문장마다 분위기 라벨(beat) 하나 붙이기.

영상 한 편에 Codex 텍스트 호출 1번. 로그인 없음·호출 실패·응답이 이상하면 규칙으로 대신한다.
결과는 작업 폴더 fx/beats.json 에 저장(문장 내용이 같으면 다시 부르지 않는다).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Callable, Optional

from .config import BEAT_LABELS

_RULES = [
    ("shock", re.compile(r"ㄷㄷ|실화|충격|헐|미쳤|대박|세상에|경악|어이없|말도 안")),
    ("funny", re.compile(r"ㅋㅋ|웃기|웃음|황당|개그|코미디")),
    ("sad", re.compile(r"ㅠ|ㅜ|슬프|안타|눈물|비극|아쉽")),
    ("angry", re.compile(r"분노|화났|열받|빡치|욕먹|논란|비난")),
    ("reveal", re.compile(r"알고 ?보니|사실은|반전|밝혀|드러났")),
]


def rule_beat(text: str, *, is_last: bool = False) -> str:
    for label, rx in _RULES:
        if rx.search(text):
            return label
    if "?" in text or "？" in text:
        return "question"
    return "conclusion" if is_last else "neutral"


def rule_beats(texts: list[str]) -> list[str]:
    return [rule_beat(t, is_last=(i == len(texts) - 1)) for i, t in enumerate(texts)]


def _digest(texts: list[str]) -> str:
    return hashlib.sha1("\n".join(texts).encode("utf-8")).hexdigest()[:16]


def build_prompt(texts: list[str]) -> str:
    lines = "\n".join(f"{i}. {t}" for i, t in enumerate(texts))
    return (
        "다음은 한국어 이슈 요약 쇼츠의 내레이션 문장 목록이다. 문장마다 그 순간의 분위기를 라벨 하나로 붙여라.\n"
        "라벨(이 중 하나만): shock(충격·놀람), funny(웃김·황당), sad(슬픔·안타까움), angry(분노·답답함), "
        "smug(득의양양·비웃음), awkward(민망·어색), reveal(반전·사실 공개), question(의문·질문), "
        "conclusion(마무리·한 줄 결론), neutral(평범한 설명)\n"
        "neutral 은 정말 밋밋한 설명 문장에만 쓰고, 감정이 느껴지면 가장 가까운 라벨을 골라라.\n"
        f'JSON 객체 하나만 답해라: {{"beats": [문장 수({len(texts)})와 같은 길이의 라벨 배열, 문장 순서대로]}}\n\n'
        f"문장:\n{lines}\n")


def validate(raw, n: int) -> Optional[list[str]]:
    """응답 → 길이 n 라벨 목록. 길이가 다르면 None, 모르는 라벨은 None 자리로 두지 않고 'neutral' 이 아닌 규칙 대체를 위해 '' 로."""
    if not isinstance(raw, dict) or not isinstance(raw.get("beats"), list) or len(raw["beats"]) != n:
        return None
    out = []
    for b in raw["beats"]:
        b = str(b).strip().lower()
        out.append(b if b in BEAT_LABELS else "")
    return out


def label_beats(texts: list[str], *, cache_path: str | Path | None = None, use_llm: bool = True,
                model: str | None = None, llm: Optional[Callable[[str], dict]] = None) -> dict:
    """반환 {"beats": [...], "source": "llm|rules|cache", "digest":…, "error":…}. llm 은 시험용 대체 호출."""
    n = len(texts)
    if model and model.lower().startswith("gemini"):
        model = None  # old local configuration follows the current Codex model
    dg = _digest(texts)
    cp = Path(cache_path) if cache_path else None
    if cp and cp.is_file():
        try:
            c = json.loads(cp.read_text(encoding="utf-8"))
            # LLM 을 썼던(성공이든 실패든) 기록이거나, 이번에 LLM 을 안 쓰면 그대로 재사용 — 실패해도 다시 부르지 않는다
            if c.get("digest") == dg and len(c.get("beats", [])) == n and (c.get("used_llm") or not use_llm):
                return {**c, "source": "cache"}
        except (OSError, ValueError):
            pass
    rules = rule_beats(texts)
    res = {"beats": rules, "source": "rules", "digest": dg, "model": None, "used_llm": bool(use_llm)}
    if use_llm and n:
        try:
            if llm is None:
                if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("RECAP_FX_NO_LLM"):
                    raise RuntimeError("시험 중이거나 RECAP_FX_NO_LLM 설정 — Codex 를 부르지 않음")
                from recap_shorts.llm.codex import generate_text

                def llm(prompt, _m=model):
                    return generate_text(prompt, model=_m, json=True, timeout=180)
            got = validate(llm(build_prompt(texts)), n)
            if got is None:
                res["error"] = "응답 모양이 맞지 않음(길이 불일치 등)"
            else:
                res = {"beats": [g or r for g, r in zip(got, rules)], "source": "llm", "digest": dg, "model": model,
                       "used_llm": True}
        except Exception as e:  # noqa: BLE001  키 없음·네트워크·한도 — 어떤 이유든 규칙으로 물러난다
            res["error"] = f"{type(e).__name__}: {str(e)[:160]}"
    if cp:
        cp.parent.mkdir(parents=True, exist_ok=True)
        cp.write_text(json.dumps({**res, "texts": texts}, ensure_ascii=False, indent=1), encoding="utf-8")
    return res
