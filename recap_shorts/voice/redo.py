"""기존 로컬 TTS 로 문장 하나를 다시 합성하고 나머지 녹음과 이어 붙인다.

    python -m recap_shorts.voice.redo <voice_dir> <index> --variant "문장|말투"

Qwen/MLX 는 기존 명령·seed 를 사용한다. VoiceDesign 의 문장별 재생성은 음색이
달라질 수 있으므로 청취 확인이 필요하다. 참조 음성은 기존 실행 명령이 관리한다.
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import soundfile as sf

from . import asr, qwen, tts


def _raw_paths(vd: Path, timings: dict) -> list[Path]:
    sents = timings["sentences"]
    saved = timings.get("raw_audio")
    if saved is not None:
        if not isinstance(saved, list) or len(saved) != len(sents):
            raise ValueError("timings.json 의 raw_audio 목록이 문장 수와 다릅니다")
        paths = [(vd / p).resolve() for p in saved]
        if any(vd not in p.parents for p in paths):
            raise ValueError("원본 음성 파일은 작업의 voice 폴더 안에 있어야 합니다")
        return paths
    # Read older local jobs without guessing a cloud provider.
    root = vd / "sentences"
    if timings.get("engine") == "qwen":
        root /= f"cut_seed{timings.get('seed', qwen.DEFAULT_SEED)}"
    return [root / f"s{i:02d}.wav" for i in range(len(sents))]


def redo(voice_dir: str | Path, index: int, variants: list[tuple[str, str]]) -> dict:
    vd = Path(voice_dir).expanduser().resolve()
    t = json.loads((vd / "timings.json").read_text(encoding="utf-8"))
    engine = t.get("engine")
    if engine not in {"qwen", "supertonic"}:
        raise ValueError("qwen/supertonic 으로 만든 로컬 음성만 다시 합성할 수 있습니다")
    sents = t["sentences"]
    if not 0 <= index < len(sents) or not variants or any(not v[0].strip() for v in variants):
        raise ValueError("문장 index 와 비어 있지 않은 variant 가 필요합니다")
    target = sents[index]
    raw_paths = _raw_paths(vd, t)
    for p in raw_paths:
        if not p.is_file():
            raise FileNotFoundError(f"다시 합성하려면 원본 문장 녹음이 필요합니다: {p}")
    redo_dir = vd / "redo" / f"s{index:02d}"
    redo_dir.mkdir(parents=True, exist_ok=True)
    has_asr = asr.available()
    todo = list(enumerate(variants)) if has_asr else list(enumerate(variants))[:1]
    trials = []
    for k, (spoken, style) in todo:
        raw = redo_dir / f"v{k}_raw.wav"
        if engine == "supertonic":
            x, sr = tts.synth_one(spoken, sid=t.get("sid"))
            sf.write(str(raw), x, sr, subtype="PCM_16")
        else:
            qwen.run_cli(spoken, style or t.get("instruct", qwen.DEFAULT_INSTRUCT),
                         int(t.get("seed", qwen.DEFAULT_SEED)), raw)
        x, sr = sf.read(str(raw), dtype="float32")
        if x.ndim > 1:
            x = x.mean(axis=1)
        x = tts.trim_silence(x, sr)
        w = redo_dir / f"v{k}.wav"
        sf.write(str(w), x, sr, subtype="PCM_16")
        heard, match = None, None
        if has_asr:
            heard = asr.transcribe(w)
            match = round(max(asr.match_ratio(target["text"], heard), asr.match_ratio(spoken, heard)), 3)
        trials.append({"variant": k, "spoken": spoken, "style": style, "heard": heard, "match": match,
                       "duration": round(len(x) / sr, 3), "wav": str(w)})
    best = max(trials, key=lambda r: (r["match"], -r["duration"])) if has_asr else trials[0]
    # Keep a complete raw set, so repeated edits do not restore a previous sentence.
    current_dir = vd / "sentences_current"
    current_dir.mkdir(exist_ok=True)
    items = []
    for i, s in enumerate(sents):
        src = Path(best["wav"]) if i == index else raw_paths[i]
        w = current_dir / f"s{i:02d}.wav"
        if src.resolve() != w.resolve():
            shutil.copyfile(src, w)
        info = sf.info(str(w))
        items.append({"text": s["text"], "spoken": best["spoken"] if i == index else s.get("spoken", s["text"]),
                      "wav": str(w), "duration": info.frames / info.samplerate})
    shutil.copyfile(vd / "timings.json", vd / "timings_before_redo.json")
    fitted = tts.fit_rate(items, vd / "sentences_fit_redo")
    warnings = list(fitted["warnings"])
    if not has_asr:
        warnings.append("받아쓰기 모델이 없어 판을 견주지 않고 첫 판을 그대로 썼습니다")
    if engine == "qwen":
        warnings.append("기존 Qwen 목소리 설정으로 다시 합성했습니다. 문장 사이 음색을 청취 확인하세요")
    keep = {k: t[k] for k in ("engine", "model", "instruct", "seed", "sid") if k in t}
    extra = {**keep, "tempo_factor": round(fitted["factor"], 4), "rate_before_fit": round(tts.speech_rate(items), 3),
             "warnings": warnings, "flags": [], "raw_audio": [str(Path(it["wav"]).relative_to(vd)) for it in items],
             "redo": {"index": index, "trials": trials, "chosen": best["variant"], "scored": has_asr}}
    timings = tts.assemble(fitted["items"], vd, roles=[s.get("role", "body") for s in sents], extra=extra)
    if has_asr:
        res, flags = asr.check_items(fitted["items"])
        for s, result in zip(timings["sentences"], res):
            s["heard"], s["match"] = result["heard"], result["match"]
        timings["flags"] = flags
        timings["asr_mean_match"] = round(sum(x["match"] for x in res) / len(res), 3) if res else None
    else:
        timings["asr_mean_match"] = None
    (vd / "timings.json").write_text(json.dumps(timings, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"trials": trials, "chosen": best, "timings": timings, "scored": has_asr}


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m recap_shorts.voice.redo")
    ap.add_argument("voice_dir")
    ap.add_argument("index", type=int)
    ap.add_argument("--variant", action="append", required=True, help='"합성할 문장|말투(비우면 기존 말투)"')
    a = ap.parse_args()
    variants = [(v.partition("|")[0], v.partition("|")[2]) for v in a.variant]
    out = redo(a.voice_dir, a.index, variants)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
