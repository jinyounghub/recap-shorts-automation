"""짤이 그 문장에 어울리는지 LLM 이 직접 고른다 (설정 memes.pick_by = "fit").

박자 라벨(beats)만 보고 고르면 평범한 사실 문장(reveal)에 주먹 내민 의기양양 페페가 붙는다 — '이겼다' 표정이 어울리지 않는 자리.
그래서 문장 전체와 짤마다의 뜻(manifest 의 meaning)을 함께 보여 주고, 반응이 자연스러운 문장에만 짤을 고르게 한다.
어울리는 곳이 없으면 짤을 안 쓴다(측정에서 35%는 0개였다).

영상 한 편에 Codex 텍스트 호출 1번. 결과는 fx/meme_fit.json 에 저장(문장·짤 목록이 같으면 다시 부르지 않는다).
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Callable, Optional


def _digest(texts: list[str], ids: list[str], eligible: list[int], max_n: int) -> str:
    raw = "\n".join(texts) + "|" + ",".join(ids) + "|" + ",".join(map(str, eligible)) + f"|{max_n}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def build_prompt(sentences: list[dict], library: list[dict], eligible: list[int], max_n: int) -> str:
    lines = "\n".join(f"{s['index']}. {s['text']}" for s in sentences)
    memes = "\n".join(f"- {it['id']}: {it.get('meaning') or it.get('name') or ''}" for it in library)
    return (
        "한국어 커뮤니티 이슈 요약 쇼츠의 내레이션 문장 목록과 반응 짤(밈 그림) 목록이다.\n"
        "짤은 그 문장이 끝난 직후 1초쯤 화면을 꽉 채워, 시청자가 속으로 하는 반응을 대신 보여 준다.\n"
        f"짤을 붙일 문장을 많아야 {max_n}개 골라라. 고를 수 있는 문장 번호: {', '.join(map(str, eligible)) or '(없음)'}\n\n"
        "고르는 기준:\n"
        "- 짤의 뜻이 그 문장 직후 시청자 반응과 딱 맞을 때만. 조금이라도 어색하면 고르지 마라.\n"
        "- 평범한 사실 설명·배경 설명 문장에는 붙이지 않는다. 황당함·어이없음·의문·분노·민망함처럼 반응이 나오는 문장에만.\n"
        "- 짤 표정이 문장의 감정과 반대거나 엉뚱하면 안 된다(예: 억울한 사실에 의기양양한 표정, 단순 정보에 분노).\n"
        "- 어울리는 곳이 없으면 빈 목록이 정답이다.\n\n"
        f"문장:\n{lines}\n\n짤:\n{memes}\n\n"
        'JSON 객체 하나만 답해라: {"picks": [{"sentence": 문장 번호, "meme": "짤 id", "why": "한 줄 이유"}, ...]} '
        "(어울리는 순서대로, 없으면 {\"picks\": []})\n")


def validate(raw, eligible: list[int], ids: list[str], max_n: int) -> Optional[list[dict]]:
    """응답 → [{"sentence","meme_id","why"}]. 모양이 틀리면 None. 고를 수 없는 문장·모르는 짤은 버리고 문장당 1개."""
    if not isinstance(raw, dict) or not isinstance(raw.get("picks"), list):
        return None
    out, seen = [], set()
    for p in raw["picks"]:
        if not isinstance(p, dict):
            continue
        try:
            k = int(p.get("sentence"))
        except (TypeError, ValueError):
            continue
        mid = str(p.get("meme") or p.get("meme_id") or "").strip()
        if k not in eligible or mid not in ids or k in seen:
            continue
        seen.add(k)
        out.append({"sentence": k, "meme_id": mid, "why": str(p.get("why") or "")[:200]})
        if len(out) >= max_n:
            break
    return out


def fit_memes(sentences: list[dict], library: list[dict], eligible: list[int], max_n: int, *,
              cache_path: str | Path | None = None, use_llm: bool = True, model: str | None = None,
              llm: Optional[Callable[[str], dict]] = None) -> dict:
    """반환 {"picks": [...], "source": "llm|cache|none", "error": …}. LLM 을 못 쓰면 picks 는 빈 목록(짤 없음).
    llm 은 시험용 대체 호출(prompt → dict)."""
    texts = [s["text"] for s in sentences]
    if model and model.lower().startswith("gemini"):
        model = None  # retain compatibility with pre-Codex local settings
    ids = [it["id"] for it in library]
    dg = _digest(texts, ids, eligible, max_n)
    cp = Path(cache_path) if cache_path else None
    if cp and cp.is_file():
        try:
            c = json.loads(cp.read_text(encoding="utf-8"))
            if c.get("digest") == dg and c.get("used_llm"):
                return {**c, "source": "cache"}
        except (OSError, ValueError):
            pass
    res: dict = {"picks": [], "source": "none", "digest": dg, "model": None, "used_llm": False}
    if not (use_llm and eligible and library and max_n > 0):
        return res
    try:
        if llm is None:
            if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("RECAP_FX_NO_LLM"):
                raise RuntimeError("시험 중이거나 RECAP_FX_NO_LLM 설정 — Codex 를 부르지 않음")
            from recap_shorts.llm.codex import generate_text

            def llm(prompt, _m=model):
                return generate_text(prompt, model=_m, json=True, timeout=180)
        got = validate(llm(build_prompt(sentences, library, eligible, max_n)), eligible, ids, max_n)
        if got is None:
            res["error"] = "응답 모양이 맞지 않음"
        else:
            res = {"picks": got, "source": "llm", "digest": dg, "model": model, "used_llm": True}
    except Exception as e:  # noqa: BLE001  키 없음·네트워크·한도 — 짤 없이 간다
        res["error"] = f"{type(e).__name__}: {str(e)[:160]}"
    if cp and res.get("used_llm"):
        cp.parent.mkdir(parents=True, exist_ok=True)
        cp.write_text(json.dumps({**res, "texts": texts}, ensure_ascii=False, indent=1), encoding="utf-8")
    return res
