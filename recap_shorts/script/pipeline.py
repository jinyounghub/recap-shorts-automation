"""topic.json -> LLM 초안 -> 가드 -> (다듬기 -> 가드) -> script_draft.json / script.json / script_check.json.

LLM 은 인자 llm 으로 바꿔 끼운다(없으면 recap_shorts.llm.make_script_llm: 기본 Codex CLI,
현재 ChatGPT OAuth 로그인과 Codex 모델 설정 사용). 다듬기(refine=True)는 초안 가드 뒤 같은 LLM 에 두 번째 호출을 하고,
결과가 초안보다 나빠지면(문장 수 불일치, 가드 삭제 증가, 호출 실패) 초안을 그대로 최종으로 쓴다.
"""
from __future__ import annotations

import json
from pathlib import Path

from .guard import as_bool, run_guard, save
from .prompt import build_prompt
from .refine import build_refine_prompt, changed_sentences, merge_refined


def normalize_script(raw: dict) -> dict:
    """모델 답을 script.json 계약 모양으로 정리 (없는 필드 채움, 문자열 다듬기)."""
    sents = []
    for s in raw.get("sentences") or []:
        if isinstance(s, str):
            s = {"text": s}
        ev = s.get("evidence")
        item = {
            "text": str(s.get("text") or "").strip(),
            "evidence": ev.strip() if isinstance(ev, str) and ev.strip() and ev.strip().lower() != "null" else None,
            "source_id": s.get("source_id") or None,
            "emphasis": s.get("emphasis") or None,
            "image_hint": str(s.get("image_hint") or "").strip(),
        }
        if "take" in s:             # F9 우리 해석 표시. 필드 자체가 없는 옛 대본·손으로 쓴 대본은 없는 채로 둔다(가드가 구분함)
            item["take"] = as_bool(s["take"])
        sents.append(item)
    tl = [str(x).strip() for x in (raw.get("title_lines") or [])][:2]
    return {"title_lines": tl, "hook": str(raw.get("hook") or "").strip(), "sentences": sents,
            "closing": str(raw.get("closing") or "").strip(), "flags": list(raw.get("flags") or [])}


def _take_ok(check: dict) -> bool:
    """가드 결과에 우리 해석(F9)이 있다고 나왔는가. 옛 check(take 항목 없음)는 없는 것으로 본다."""
    return bool(((check.get("checks") or {}).get("take") or {}).get("ok"))


def refine_script(topic: dict, draft: dict, draft_check: dict, llm) -> tuple[dict, dict, dict]:
    """(최종 script, 최종 check, 다듬기 기록). 실패하면 초안을 그대로 돌려준다."""
    info: dict = {"applied": False, "error": None, "changed": []}
    try:
        raw = llm.complete_json(build_refine_prompt(topic, draft))
    except Exception as e:  # noqa: BLE001  화면·기록에는 짧은 한국어 이유만
        from recap_shorts import errors
        info["error"] = f"다듬기 호출 실패 — {errors.screen_text(e)}"
        return draft, draft_check, info
    merged, why = merge_refined(draft, raw if isinstance(raw, dict) else {})
    if merged is None:
        info["error"] = why
        return draft, draft_check, info
    script, check = run_guard(normalize_script(merged), topic)
    if len(check["deleted"]) > len(draft_check["deleted"]):
        info["error"] = f"다듬기 결과가 가드에서 {len(check['deleted'])}문장 삭제돼 초안을 씀"
        return draft, draft_check, info
    if _take_ok(draft_check) and not _take_ok(check):
        info["error"] = "다듬기 결과에서 우리 해석 문장이 사라지거나 원문 복사가 돼 초안을 씀"
        return draft, draft_check, info
    info["applied"] = True
    info["changed"] = changed_sentences(draft, script)
    return script, check, info


def generate_script(topic: dict, out_dir: str | Path, llm=None, refine: bool = True, on_stage=None) -> tuple[dict, dict]:
    """on_stage(이름, 상태, 메모): "draft"|"refine" 단계의 start/done/error 알림(앱 진행 표시용, 없어도 됨)."""
    def stage(name, status, note=None):
        if on_stage:
            on_stage(name, status, note)

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    if llm is None:
        from recap_shorts.llm import make_script_llm
        llm = make_script_llm()
    prompt = build_prompt(topic)
    (out / "prompt.txt").write_text(prompt, encoding="utf-8")
    stage("draft", "start")
    try:
        raw = llm.complete_json(prompt)
    except Exception as e:  # noqa: BLE001  화면에 가는 소항목 메모 — 예외 이름은 넣지 않는다
        from recap_shorts import errors
        stage("draft", "error", errors.screen_text(e))
        raise
    stage("draft", "done", f"{llm.calls[-1].get('seconds', '?')}초" if getattr(llm, "calls", None) else None)
    (out / "llm_raw.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    draft, draft_check = run_guard(normalize_script(raw), topic)
    (out / "script_draft.json").write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "script_draft_check.json").write_text(json.dumps(draft_check, ensure_ascii=False, indent=2), encoding="utf-8")
    script, check = draft, draft_check
    if refine:
        stage("refine", "start")
        script, check, info = refine_script(topic, draft, draft_check, llm)
        check = dict(check)
        check["refine"] = info
        secs = llm.calls[-1].get("seconds", "?") if getattr(llm, "calls", None) else "?"
        if info["applied"]:
            stage("refine", "done", f"{secs}초 · {len(info['changed'])}곳 고침")
        else:
            stage("refine", "error", info["error"])
    check["llm_calls"] = list(llm.calls)
    save(script, check, out)
    return script, check
