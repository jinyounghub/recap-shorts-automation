"""script.json -> 문장별 합성 -> 속도 맞춤 -> 이어 붙이기 -> 받아쓰기 확인."""
from __future__ import annotations

import json
from pathlib import Path

from recap_shorts.script.guard import narration_texts

from . import tts


def narration_roles(script: dict) -> tuple[list[str], list[str]]:
    texts = narration_texts(script)
    roles = []
    has_hook = bool(script.get("hook"))
    n_body = len(script.get("sentences") or [])
    if has_hook:
        roles.append("hook")
    roles += ["body"] * n_body
    if script.get("closing"):
        roles.append("closing")
    return texts, roles


def make_voice(script: dict, out_dir: str | Path, sid: int | None = None, check_asr: bool = True,
               fit: bool = True, engine: str = "qwen", instruct: str | None = None,
               seed: int | None = None) -> dict:
    out = Path(out_dir).expanduser().resolve()
    texts, roles = narration_roles(script)
    engine_meta: dict = {}
    if engine == "qwen":
        from . import qwen
        r = qwen.synth_sentences(texts, out / "sentences", instruct=instruct, seed=seed, check=check_asr)
        raw, engine_meta = r["items"], r["meta"]
    elif engine == "supertonic":
        raw = tts.synth_sentences(texts, out / "sentences", sid=sid)
    else:
        raise ValueError(f"알 수 없는 엔진: {engine}")
    warnings: list[str] = []
    clipped = []
    if engine == "qwen":          # 한 번에 합성한 녹음을 자른 조각: 경계가 낱말 속에 들어가면 말끝이 잘린다
        for i, it in enumerate(raw):
            d = tts.end_level_db(it["wav"])
            if d > tts.END_CLIP_DB:
                clipped.append(i)
                warnings.append(f"'…{it['text'][-10:]}' 끝이 말 중간에서 잘렸을 수 있음(끝 소리 {d:.0f} dB) — 들어 보고 이 문장만 목소리 다시")
    factor, before = 1.0, tts.speech_rate(raw)
    items = raw
    if fit:
        r = tts.fit_rate(raw, out / "sentences_fit")
        items, factor = r["items"], r["factor"]
        warnings.extend(r["warnings"])
    extra = {"engine": engine, "tempo_factor": round(factor, 4),
             "rate_before_fit": round(before, 3), "warnings": warnings, "flags": [], "clipped_ends": clipped,
             "raw_audio": [str(Path(it["wav"]).resolve().relative_to(out)) for it in raw]}
    if engine == "qwen":
        extra.update({k: engine_meta[k] for k in ("instruct", "seed", "tries", "retries", "model", "attempts")})
        if not engine_meta["min_match_ok"]:
            warnings.append("Qwen 자르기/발음 확인이 재시도 후에도 일치율 기준에 못 미침 (attempts 참고)")
    else:
        extra["sid"] = tts.default_sid() if sid is None else sid
    timings = tts.assemble(items, out, roles=roles, extra=extra)
    if check_asr:
        from . import asr
        if not asr.available():
            # 받아쓰기 모델이 없는 PC(새로 받은 저장소): 발음 확인만 건너뛰고 영상은 그대로 만든다
            warnings.append("받아쓰기 모델이 없어 발음 확인을 건너뛰었습니다 — 기존 SenseVoice 모델은 "
                            "RECAP_SENSEVOICE_DIR 로 연결하세요. 모델을 자동으로 받지 않습니다.")
            timings["asr_mean_match"] = None
            (out / "timings.json").write_text(json.dumps(timings, ensure_ascii=False, indent=2), encoding="utf-8")
            return timings
        res, flags = asr.check_items(items)
        for s, r in zip(timings["sentences"], res):
            s["heard"], s["match"] = r["heard"], r["match"]
            s["match_text"], s["match_spoken"] = r["match_text"], r["match_spoken"]
        timings["flags"] = flags
        timings["asr_mean_match"] = round(sum(r["match"] for r in res) / len(res), 3) if res else None
        (out / "timings.json").write_text(json.dumps(timings, ensure_ascii=False, indent=2), encoding="utf-8")
    return timings
