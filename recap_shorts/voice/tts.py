"""Supertonic-3 로컬 TTS (sherpa-onnx) + 말 속도 맞추기 + 이어 붙이기.

모델 폴더는 저장소 안 models/(scripts/setup_models.py --supertonic 로 받음). 환경변수 RECAP_SUPERTONIC_DIR 로 바꿀 수 있다.
설정(환경변수): RECAP_TTS_SID(화자 번호, 기본 0), RECAP_TTS_STEPS(기본 8), RECAP_TTS_THREADS(기본 2).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

import numpy as np
import soundfile as sf

from recap_shorts import modelfiles
from recap_shorts.script.text import count_syllables

from .spoken import to_spoken

TARGET_RATE = (6.3, 6.7)          # 음절/초 (docs/02 규격표, 중앙 6.53)
TEMPO_RANGE = (0.8, 1.25)         # atempo 허용 범위
LEAD_IN = 0.1                     # 첫 말 시작 시각(초, 규격 0.15 이하)
PAUSE_RANGE = (0.08, 0.15)

_LOCK = threading.Lock()
_CACHE: dict[str, object] = {}


class TTSUnavailable(RuntimeError):
    pass


def model_dir() -> Path:
    return modelfiles.model_dir("supertonic")


def default_sid() -> int:
    """목소리 번호: 환경변수 RECAP_TTS_SID → 설정 화면에서 고른 번호 → 0."""
    from recap_shorts import settings
    return settings.supertonic_sid()


def trim_silence(x: np.ndarray, sr: int, thresh: float = 0.01, pad: float = 0.04) -> np.ndarray:
    """앞뒤 무음 잘라내기."""
    idx = np.where(np.abs(x) > thresh)[0]
    if len(idx) == 0:
        return x
    a = max(0, idx[0] - int(pad * sr))
    b = min(len(x), idx[-1] + int(pad * sr))
    return x[a:b]


END_CLIP_DB = -36.0   # 조각 끝 40ms 의 세기가 이보다 크면 말 중간에서 잘린 것으로 본다(깨끗한 끝 −46 dB 아래, 숨소리 −39 dB 안팎)


def end_level_db(wav: str | Path, tail: float = 0.04) -> float:
    """소리 조각 끝 tail 초의 세기(dBFS, RMS). 한 번에 합성한 녹음을 문장별로 자른 조각의 말끝이 잘렸는지 볼 때 쓴다."""
    x, sr = sf.read(str(wav), dtype="float32")
    if x.ndim > 1:
        x = x.mean(axis=1)
    t = x[-max(1, int(tail * sr)):]
    return float(20 * np.log10(np.sqrt(np.mean(t ** 2)) + 1e-9))


def _engine(d: Path):
    key = str(d)
    with _LOCK:
        if key not in _CACHE:
            try:
                import sherpa_onnx as so
            except ImportError as e:  # pragma: no cover
                raise TTSUnavailable("pip install -r requirements-voice.txt") from e
            if not (d / "vocoder.int8.onnx").exists():
                raise TTSUnavailable(f"Supertonic 모델 없음: {d} — 기존 모델을 RECAP_SUPERTONIC_DIR 로 연결하세요")
            cfg = so.OfflineTtsConfig(
                model=so.OfflineTtsModelConfig(
                    supertonic=so.OfflineTtsSupertonicModelConfig(
                        duration_predictor=str(d / "duration_predictor.int8.onnx"),
                        text_encoder=str(d / "text_encoder.int8.onnx"),
                        vector_estimator=str(d / "vector_estimator.int8.onnx"),
                        vocoder=str(d / "vocoder.int8.onnx"),
                        tts_json=str(d / "tts.json"),
                        unicode_indexer=str(d / "unicode_indexer.bin"),
                        voice_style=str(d / "voice.bin"),
                    ),
                    num_threads=int(os.environ.get("RECAP_TTS_THREADS", "2")), provider="cpu", debug=False,
                )
            )
            if not cfg.validate():
                raise TTSUnavailable("Supertonic 설정이 올바르지 않음")
            _CACHE[key] = so.OfflineTts(cfg)
        return _CACHE[key]


def synth_one(text: str, sid: int | None = None, speed: float = 1.0, num_steps: int | None = None,
              model_path: Path | None = None) -> tuple[np.ndarray, int]:
    import sherpa_onnx as so
    tts = _engine(model_path or model_dir())
    gc = so.GenerationConfig()
    gc.sid = default_sid() if sid is None else sid
    if not 0 <= gc.sid <= 9:      # 모델에 화자 10명(0~9). 범위 밖이면 sherpa 가 조용히 0번으로 바꾼다
        raise ValueError(f"sid 는 0~9 여야 함: {gc.sid}")
    gc.speed = float(speed)
    gc.num_steps = num_steps or int(os.environ.get("RECAP_TTS_STEPS", "8"))
    gc.extra["lang"] = "ko"
    with _LOCK:
        audio = tts.generate(text, gc)
    x = trim_silence(np.asarray(audio.samples, dtype=np.float32), audio.sample_rate)
    return x, audio.sample_rate


def synth_sentences(sentences: list[str], out_dir: str | Path, sid: int | None = None, speed: float = 1.0,
                    num_steps: int | None = None) -> list[dict]:
    """문장마다 합성 -> 앞뒤 무음 제거 -> out_dir/s00.wav ... 반환: [{text, wav, duration}]"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    items = []
    for i, t in enumerate(sentences):
        spoken = to_spoken(t)          # 숫자·약어는 한글 읽기로 바꿔 합성, 원문(text)은 그대로 기록
        x, sr = synth_one(spoken, sid, speed, num_steps)
        p = out / f"s{i:02d}.wav"
        sf.write(str(p), x, sr, subtype="PCM_16")
        items.append({"text": t, "spoken": spoken, "wav": str(p), "duration": len(x) / sr})
    return items


# ---------------------------------------------------------------- 속도 맞추기

def _ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise TTSUnavailable("ffmpeg 를 찾을 수 없음")
    return exe


def pause_after(text: str) -> float:
    """문장 뒤 쉼(초). 0.08~0.15 안. 물음·감탄·줄임표 뒤는 조금 길게."""
    t = text.rstrip()
    return 0.14 if t.endswith(("?", "!", "…", "...", "？", "！")) else 0.10


def atempo(src: str | Path, dst: str | Path, factor: float) -> float:
    """ffmpeg atempo 로 속도를 factor 배로. 새 길이(초)를 돌려준다."""
    subprocess.run([_ffmpeg(), "-y", "-loglevel", "error", "-i", str(src), "-filter:a", f"atempo={factor:.5f}",
                    "-c:a", "pcm_s16le", str(dst)], check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    info = sf.info(str(dst))
    return info.frames / info.samplerate


def speech_rate(items: list[dict]) -> float:
    """조립했을 때의 말 속도(음절/초): 첫 말 시작부터 끝까지, 문장 사이 쉼 포함."""
    syl = sum(count_syllables(it["text"]) for it in items)
    total = sum(it["duration"] for it in items) + sum(pause_after(it["text"]) for it in items[:-1])
    return syl / total if total else 0.0


def fit_rate(items: list[dict], out_dir: str | Path, target: tuple[float, float] = TARGET_RATE,
             tempo_range: tuple[float, float] = TEMPO_RANGE) -> dict:
    """전체 말 속도를 target 안으로. 이미 안이면 그대로. 아니면 목표 중앙값에 맞는 배율을 구해 atempo 적용.
    배율이 tempo_range 를 벗어나면 범위 끝으로 제한하고 경고. 반환: {items, factor, rate_before, rate_after, warnings}"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    before = speech_rate(items)
    warnings: list[str] = []
    if target[0] <= before <= target[1]:
        return {"items": items, "factor": 1.0, "rate_before": before, "rate_after": before, "warnings": warnings}
    goal = (target[0] + target[1]) / 2
    syl = sum(count_syllables(it["text"]) for it in items)
    pauses = sum(pause_after(it["text"]) for it in items[:-1])
    speech = sum(it["duration"] for it in items)
    room = syl / goal - pauses
    factor = speech / room if room > 0 else tempo_range[1]
    clamped = min(max(factor, tempo_range[0]), tempo_range[1])
    if clamped != factor:
        warnings.append(f"필요한 배율 {factor:.3f} 이 허용 범위 {tempo_range} 를 벗어나 {clamped:.3f} 로 제한함 "
                        f"(대본 길이 조정 필요: 음절 {syl})")
    new_items = []
    for it in items:
        dst = out / Path(it["wav"]).name
        dur = atempo(it["wav"], dst, clamped)
        new_items.append({**it, "wav": str(dst), "duration": dur})
    after = speech_rate(new_items)
    if not (target[0] <= after <= target[1]):
        warnings.append(f"말 속도 {after:.2f} 음절/초 가 목표 {target} 밖")
    return {"items": new_items, "factor": clamped, "rate_before": before, "rate_after": after, "warnings": warnings}


# ---------------------------------------------------------------- 이어 붙이기

def assemble(items: list[dict], out_dir: str | Path, roles: list[str] | None = None, lead_in: float = LEAD_IN,
             tail: float = 0.25, extra: dict | None = None) -> dict:
    """문장 wav 들을 이어 voice.wav 와 timings.json 으로. 첫 말은 lead_in 초에 시작, 문장 사이 쉼은 pause_after.
    짧은 페이드(5ms)로 이음매 잡음을 막는다. 반환: timings dict."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    chunks, sr = [], None
    for it in items:
        x, r = sf.read(it["wav"], dtype="float32")
        if x.ndim > 1:
            x = x.mean(axis=1)
        if sr is None:
            sr = r
        elif r != sr:
            raise ValueError(f"샘플레이트가 다름: {r} != {sr}")
        chunks.append(x)
    sr = sr or 44100
    fade = int(0.005 * sr)
    pos = int(round(lead_in * sr))
    parts = [np.zeros(pos, dtype=np.float32)]
    sents = []
    for i, (it, x) in enumerate(zip(items, chunks)):
        x = x.copy()
        if len(x) > 2 * fade:
            x[:fade] *= np.linspace(0, 1, fade, dtype=np.float32)
            x[-fade:] *= np.linspace(1, 0, fade, dtype=np.float32)
        start = pos / sr
        parts.append(x)
        pos += len(x)
        sents.append({"index": i, "role": (roles[i] if roles else "body"), "text": it["text"],
                      "spoken": it.get("spoken", it["text"]),
                      "start": round(start, 3), "end": round(pos / sr, 3), "syllables": count_syllables(it["text"])})
        if i < len(items) - 1:
            gap = int(round(pause_after(it["text"]) * sr))
            parts.append(np.zeros(gap, dtype=np.float32))
            pos += gap
    parts.append(np.zeros(int(tail * sr), dtype=np.float32))
    audio = np.concatenate(parts)
    wav = out / "voice.wav"
    sf.write(str(wav), audio, sr, subtype="PCM_16")
    syl = sum(s["syllables"] for s in sents)
    span = sents[-1]["end"] - sents[0]["start"] if sents else 0.0
    timings = {"voice": "voice.wav", "sample_rate": sr, "duration": round(len(audio) / sr, 3),
               "speech_start": sents[0]["start"] if sents else 0.0, "speech_end": sents[-1]["end"] if sents else 0.0,
               "syllables": syl, "rate": round(syl / span, 3) if span else 0.0, "sentences": sents}
    if extra:
        timings.update(extra)
    (out / "timings.json").write_text(json.dumps(timings, ensure_ascii=False, indent=2), encoding="utf-8")
    return timings
