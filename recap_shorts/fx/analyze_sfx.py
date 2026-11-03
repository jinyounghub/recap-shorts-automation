"""효과음 폴더 분석: 재기(무료) → 로컬 오디오 모델 듣기 → 종류별 고르기 → 다듬어 꾸러미 만들기 (docs/library_analysis.md).

원본 폴더는 읽기만 한다. 꾸러미(manifest.json + 다듬은 wav)는 fx/library.py 가 정한 자리에 만든다.
규칙 값은 config/fx.json 의 analyze.sfx (없으면 아래 기본값).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional

from . import sfx as S

SR = 48000
HOP = 480            # 10 ms
PEAK = 10 ** (-1 / 20)  # 다듬은 소리의 피크 −1 dBFS

# 듣기 프롬프트에 넣는 종류 설명(영어가 소리 분류에 더 정확했다) — 화면 이름은 TYPE_LABEL
TYPE_DESC = {
    "hit": "very short dry impact/slap/knock for the very start of a video (under 0.3 s)",
    "pop": "short cartoon pop / bonk / boop / bubble, light and neutral, used on picture cuts",
    "ding": "bright single bell / notification ding",
    "sparkle": "shimmering chime / twinkle / magic glitter",
    "boom": "deep heavy impact or bass drop (vine-boom style) for shock or reveal",
    "huh": "confused 'huh?' / 'what?' reaction",
    "scream": "angry or shocked scream / yell",
    "awkward": "awkward moment: bruh, record scratch, crickets, silence-ish",
    "sad": "sad trombone, womp womp, crying, disappointed",
    "laugh": "laughter or funny reaction",
    "sus": "suspicious / smug / sneaky reaction",
    "punchline": "rimshot / ba-dum-tss / ta-da / conclusion sting",
    "whoosh": "air whoosh / swish for a transition",
    "whoosh_short": "very short swish (under 0.4 s)",
    "swoosh": "soft swoosh for a transition",
    "click": "mouse or button click",
    "typing": "keyboard typing",
    "riser": "rising tension sweep",
    "buzz": "error buzzer / wrong answer",
    "boing": "spring boing / cartoon bounce",
    "shutter": "camera shutter",
    "coin": "coin / reward pickup",
    "slidewhistle": "slide whistle up or down",
    "cheer": "crowd cheer / applause",
    "oooh": "crowd 'oooh' reaction",
    "aw": "crowd 'aww' sympathy",
    "gasp": "gasp of surprise",
    "error": "error / system alert",
    "bleep": "censor bleep",
    "fanfare": "short victory fanfare",
    "oof": "'oof' pain reaction",
    "jingle": "short jingle",
}
TYPE_LABEL = {
    "hit": "탁 (영상 시작)", "pop": "뽁·톡 (컷)", "ding": "딩 (컷)", "sparkle": "반짝 (컷)", "boom": "쿵 (충격·반전)",
    "huh": "엥? (의문)", "scream": "비명 (분노)", "awkward": "어색", "sad": "시무룩", "laugh": "웃음", "sus": "수상·으쓱",
    "punchline": "마무리 한 방", "whoosh": "휙", "whoosh_short": "짧은 휙", "swoosh": "슉", "click": "딸깍", "typing": "타자",
    "riser": "긴장 올림", "buzz": "삑 (틀림)", "boing": "띠용", "shutter": "찰칵", "coin": "띠링 (보상)",
    "slidewhistle": "미끄럼 휘슬", "cheer": "환호", "oooh": "오~", "aw": "아~", "gasp": "헉", "error": "경고음",
    "bleep": "삐 처리", "fanfare": "팡파레", "oof": "윽", "jingle": "짧은 음악",
}
# 짧아야 하는 자리(낱말 대사가 들어가면 감점)
SHORT_TYPES = {"hit", "pop", "ding", "sparkle", "boom", "whoosh_short", "click", "coin"}
# 사람이 정한 종류를 바꾸는 화면에서 고를 수 있는 것 = 규칙이 쓰는 종류(+ 이미 들어 있는 종류)

DEFAULTS = {
    "max_files": 800,          # 폴더 하나에서 볼 소리 파일 수
    "max_file_s": 40.0,        # 이보다 긴 파일은 효과음이 아니라고 보고 듣지 않는다
    "listen_s": 6.0,           # 들려줄 길이(소리 시작부터)
    "min_fit": 2,              # 종류 어울림 0~3 중 이 이상만 후보
    "min_quality": 3,          # 품질 1~5 중 이 이상만
    "max_clip_ratio": 0.02,    # 찢어진(|x|>0.99) 표본 비율
    "drop_owner_clear": True,  # 게임·브랜드·방송·노래처럼 권리자가 분명한 소리 빼기
    "want": {"pop": 4, "*": 3},
    "max_active_s": {"hit": 0.6, "pop": 1.0, "ding": 2.0, "sparkle": 2.5, "boom": 3.0, "*": 3.5},
    "cap_s": {"hit": 0.35, "pop": 0.6, "ding": 1.2, "sparkle": 1.0, "boom": 1.6, "huh": 1.3, "scream": 1.6,
              "awkward": 1.8, "sad": 2.2, "laugh": 2.0, "sus": 1.8, "punchline": 1.8, "*": 1.8},
}
BAD_SOURCE = {"brand", "game", "song", "streamer_or_celebrity", "tv_movie_anime"}


def opts(cfg: dict) -> dict:
    """config/fx.json analyze.sfx 를 기본값 위에 얹은 것. 사전 값(want·max_active_s·cap_s)은 키별로 합친다."""
    got = ((cfg.get("analyze") or {}).get("sfx") or {})
    out = {**DEFAULTS, **{k: v for k, v in got.items() if not str(k).startswith("_")}}
    for k in ("want", "max_active_s", "cap_s"):
        out[k] = {**DEFAULTS[k], **(got.get(k) or {})}
    return out


def per_type(table: dict, typ: str):
    return table.get(typ, table.get("*"))


def wanted_types(scfg: dict) -> list[str]:
    """규칙(효과음 자리)이 쓰는 종류 — 듣기 프롬프트와 화면 묶음 순서. 설명이 없는 종류는 뺀다."""
    order = list(TYPE_DESC)
    got = S.slot_types(scfg)
    return [t for t in order if t in got]


# ------------------------------------------------------------------ 재기

def list_files(src: Path, max_files: int, skip: Path | None = None) -> tuple[list[Path], int]:
    """소리 파일(하위 폴더 두 단계까지, 이름순). (목록, 너무 많아 뺀 수). skip 안의 파일은 보지 않는다."""
    found = []
    for pat in ("*", "*/*", "*/*/*"):
        for p in sorted(src.glob(pat)):
            if p.is_file() and p.suffix.lower() in S.AUDIO_EXTS and not p.name.startswith("_들어보기"):
                if skip is not None:
                    try:
                        p.resolve().relative_to(skip.resolve())
                        continue
                    except ValueError:
                        pass
                found.append(p)
    return found[:max_files], max(0, len(found) - max_files)


def decode(path: str | Path, sr: int = SR):
    """ffmpeg 로 모노 float32 로 푼다. 못 풀면 빈 배열."""
    import numpy as np
    from recap_shorts.render.audio import ffmpeg_bin
    r = subprocess.run([ffmpeg_bin(), "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
                       capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return np.frombuffer(r.stdout, dtype=np.float32)


def features(x, sr: int = SR) -> dict:
    """길이, 소리 시작(최대 −20 dB 를 처음 넘는 곳), 소리 끝(−30 dB 를 마지막으로 넘는 곳), 피크, 찢어짐 비율, 밝기."""
    import numpy as np
    if x.size == 0:
        return {"error": "소리를 읽지 못함"}
    n = max(1, x.size // HOP)
    rms = np.sqrt(np.mean(x[: n * HOP].reshape(n, HOP) ** 2, axis=1) + 1e-12) if x.size >= HOP else np.array([np.sqrt(np.mean(x ** 2) + 1e-12)])
    db = 20 * np.log10(rms + 1e-12)
    top = float(db.max())
    act = np.where(db > top - 30)[0]
    on = np.where(db > top - 20)[0]
    dur = x.size / sr
    out = {"dur": round(dur, 3), "peak_db": round(float(20 * np.log10(np.abs(x).max() + 1e-12)), 2),
           "onset": round(float(on[0] * HOP / sr), 3) if on.size else 0.0,
           "active_end": round(float((act[-1] + 1) * HOP / sr), 3) if act.size else round(dur, 3),
           "clip_ratio": round(float(np.mean(np.abs(x) > 0.99)), 4)}
    out["active_end"] = min(out["active_end"], out["dur"])
    out["active_len"] = round(max(0.0, out["active_end"] - out["onset"]), 3)
    if top < -60:
        out["error"] = "소리가 거의 없음"
        return out
    out["sig"] = signature(x, out["onset"], sr)
    return out


def signature(x, onset: float, sr: int = SR) -> dict:
    """비슷한 소리(같은 밈을 다른 이름으로 올린 것) 찾기용: 소리 시작부터 1초의 세기 모양(40 ms × 25)과
    48개 대역 세기(로그 간격 60 Hz~11 kHz, 대역 하나가 반음 두 개쯤 — 낮은 비트레이트 mp3 가 잘라 내는 높은 소리는 뺌).
    둘 다 0~1 로 맞춘다."""
    import numpy as np
    seg = x[int(onset * sr): int((onset + 1.0) * sr)]
    if seg.size < 64:
        seg = x
    hop = int(0.04 * sr)
    env = [float(np.sqrt(np.mean(seg[i:i + hop] ** 2))) if i < seg.size else 0.0 for i in range(0, hop * 25, hop)]
    top = max(env) or 1.0
    spec = np.abs(np.fft.rfft(seg * np.hanning(seg.size)))
    f = np.fft.rfftfreq(seg.size, 1 / sr)
    edges = np.geomspace(60, 11000, 49)
    bands = [float(spec[(f >= a) & (f < b)].sum()) for a, b in zip(edges, edges[1:])]
    btop = max(bands) or 1.0
    return {"env": [round(v / top, 3) for v in env], "bands": [round(v / btop, 3) for v in bands]}


def _corr(a: list, b: list) -> float:
    import numpy as np
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    if a.size != b.size or a.std() < 1e-6 or b.std() < 1e-6:
        return 0.0
    return float(np.corrcoef(a, b)[0, 1])


def similar(m1: dict, m2: dict) -> bool:
    """같은 소리를 다시 인코딩한 정도로 닮았는가. 첫 1초가 거의 같으면(세기 모양 0.98·대역 0.99 이상) 길이가 달라도
    같은 소리(꼬리만 더 길게 자른 사본), 아니면 소리 길이 15% 안 + 세기 모양 0.95·대역 0.97 이상."""
    s1, s2 = m1.get("sig"), m2.get("sig")
    if not s1 or not s2:
        return False
    env, bands = _corr(s1["env"], s2["env"]), _corr(s1["bands"], s2["bands"])
    if env >= 0.98 and bands >= 0.99:
        return True
    l1, l2 = float(m1.get("active_len", 0)), float(m2.get("active_len", 0))
    if max(l1, l2) <= 0 or abs(l1 - l2) / max(l1, l2) > 0.15:
        return False
    return env >= 0.95 and bands >= 0.97


def file_sig(p: Path) -> str:
    """다시 분석할 때 같은 파일인지 볼 표: 크기 + 수정 시각."""
    st = p.stat()
    return f"{st.st_size}:{int(st.st_mtime)}"


def content_hash(p: Path) -> str:
    h = hashlib.sha1()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def measure(p: Path) -> dict:
    try:
        x = decode(p)
    except (OSError, RuntimeError) as e:
        return {"error": f"읽지 못함({type(e).__name__})"}
    return features(x)


# ------------------------------------------------------------------ 듣기

def build_prompt(types: list[str]) -> str:
    return (
        "You will hear several short sound clips from a user's sound-effect folder. Each clip is preceded by its label "
        "\"#k\" and file name.\nThey will be used as sound effects in Korean YouTube Shorts that summarize community posts. "
        "Slot types:\n" + "\n".join(f"- {t}: {TYPE_DESC[t]}" for t in types) + "\n\n"
        "For EACH clip return one JSON object, all in one JSON array:\n"
        '{"k": <label number>, "desc": "<what you hear, in Korean, max 40 chars>",\n'
        ' "speech": "none" | "word" | "sentence", "words": "<transcript if any speech, else empty>", "lang": "<speech language or empty>",\n'
        ' "music": true if it contains a melody/song/instrumental music bed (a single bell or chime note is NOT music),\n'
        ' "source": "generic" | "meme" | "game" | "brand" | "tv_movie_anime" | "streamer_or_celebrity" | "song",\n'
        ' "owner_clear": true if the clip is clearly recognizable as a specific copyrighted work, brand jingle, game sound, song, or a famous person\'s voice,\n'
        ' "offensive": true if slurs, sexual sounds, hate, or insults,\n'
        ' "earrape": true if intentionally distorted / extremely loud,\n'
        ' "fit": {"<type>": 0-3, ...} only for the slot types above with fit >= 1,\n'
        ' "quality": 1-5 (clean recording, usable as a short effect)}\n'
        "Return only the JSON array.")


def clip_b64(path: str | Path, onset: float, seconds: float) -> str:
    from recap_shorts.render.audio import ffmpeg_bin
    st = max(0.0, float(onset) - 0.05)
    r = subprocess.run([ffmpeg_bin(), "-v", "error", "-ss", f"{st:.3f}", "-i", str(path), "-t", f"{seconds:.2f}",
                        "-ac", "1", "-ar", "16000", "-f", "wav", "-"],
                       capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return base64.b64encode(r.stdout).decode("ascii")


HEARD_KEYS = ("desc", "speech", "words", "lang", "music", "source", "owner_clear", "offensive", "earrape", "fit", "quality")


def parse_answer_list(text: str) -> list:
    """모델 답 → 객체 목록. JSON 배열 그대로, ```json 울타리, {"items": [...]} 처럼 감싼 것, 앞뒤 글을 허용한다.
    (script.llm.parse_json_loose 는 객체 하나만 꺼내서 배열 답의 첫 항목만 남는다.)"""
    text = re.sub(r"<think>.*?(</think>|$)", "", text or "", flags=re.S).strip()
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.S)
    if m:
        text = m.group(1)
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch not in "[{":
            continue
        try:
            obj, _ = dec.raw_decode(text[i:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, list):
            return obj
        if isinstance(obj, dict):
            inner = next((v for v in obj.values() if isinstance(v, list)), None)
            return inner if inner is not None else [obj]
    return []


def clean_heard(r: dict, types: list[str]) -> dict:
    """모델 답 한 개를 정해진 모양으로(모르는 값은 버림)."""
    fit = {}
    for t, v in (r.get("fit") or {}).items() if isinstance(r.get("fit"), dict) else []:
        if t in types and isinstance(v, (int, float)) and 0 < v <= 3:
            fit[t] = int(round(v))
    q = r.get("quality")
    return {"desc": str(r.get("desc") or "")[:60],
            "speech": r.get("speech") if r.get("speech") in ("none", "word", "sentence") else "none",
            "words": str(r.get("words") or "")[:80], "lang": str(r.get("lang") or "")[:12],
            "music": bool(r.get("music")), "source": str(r.get("source") or "generic")[:30],
            "owner_clear": bool(r.get("owner_clear")), "offensive": bool(r.get("offensive")),
            "earrape": bool(r.get("earrape")), "fit": fit,
            "quality": int(q) if isinstance(q, (int, float)) and 1 <= q <= 5 else 3}


def listen_batch(batch: list[tuple[str, Path, float]], types: list[str], *, model: str | None, listen_s: float,
                 llm: Optional[Callable[[list], object]] = None) -> dict[str, dict]:
    """Injected local listener only; Codex vision cannot listen to audio clips.

    Default production uses local measurements, names, and manual manifest tags.
    A caller may inject its existing local audio model through ``llm``.
    """
    if llm is None:
        raise RuntimeError("외부 음성 분석 API는 사용하지 않습니다. 로컬 측정값과 파일명·manifest 태그를 사용하세요.")
    parts: list[dict] = [{"text": build_prompt(types)}]
    for k, (key, p, onset) in enumerate(batch, 1):
        parts.append({"text": f'#{k} file: "{Path(key).stem}"'})
        parts.append({"inline_data": {"mime_type": "audio/wav", "data": clip_b64(p, onset, listen_s)}})
    res = llm(parts)
    if isinstance(res, str):
        res = parse_answer_list(res)
    elif isinstance(res, dict):       # {"items": [...]} 처럼 감싸 오는 경우
        res = next((v for v in res.values() if isinstance(v, list)), [res])
    out: dict[str, dict] = {}
    for r in res if isinstance(res, list) else []:
        key = batch_key(r, batch)
        if key is not None:
            out[key] = clean_heard(r, types)
    return out


def batch_key(r, batch: list) -> str | None:
    """답 한 개의 "k"(1부터, "#3" 처럼 와도 됨) → 그 묶음의 key."""
    try:
        k = int(str(r.get("k")).lstrip("#").strip())
    except (AttributeError, ValueError, TypeError):
        return None
    return batch[k - 1][0] if 1 <= k <= len(batch) else None


# ------------------------------------------------------------------ 고르기

def norm_name(name: str) -> str:
    """'뽁 (2).mp3', '뽁_02' 같은 사본을 같은 이름으로 본다."""
    t = re.sub(r"[^0-9a-z가-힣ぁ-んァ-ン一-龥]", "", Path(name).stem.lower())
    t = re.sub(r"\d+$", "", t)
    return re.sub(r"(.)\1+", r"\1", t)[:16]


def drop_reasons(it: dict, o: dict, types: list[str]) -> list[str]:
    """이 파일을 쓰지 않을 이유(빈 목록이면 후보). 파일 이름에 영상 규칙이 쓰는 종류가 적힌 파일('pop_01.wav')은
    이름이 종류를 정하지만, 들은 결과가 있으면 빼는 규칙(욕설·음악·대사·품질 …)은 똑같이 건다 — 내려받은 파일 이름
    ('sad-meow-song.mp3')은 종류 이름이 우연히 들어 있을 뿐인 경우가 많다. 들은 결과가 없으면(키 없음) 이름을 믿는다.
    규칙이 안 쓰는 종류 이름('whoosh-sound-effect.mp3')은 이름을 믿지 않고 들은 결과로 고른다."""
    m = it.get("measure") or {}
    if m.get("error"):
        return [m["error"]]
    if m.get("dur", 0) > float(o["max_file_s"]):
        return ["너무 긴 파일(효과음 아님)"]
    h = it.get("heard")
    if h is None:
        if it.get("name_type") in types:
            return []
        if it.get("listen_error"):
            return ["듣지 못함"]
        return [f"영상 규칙이 안 쓰는 종류({it['name_type']})"] if it.get("name_type") else ["이름으로 종류를 알 수 없음"]
    why = []
    if h.get("offensive"):
        why.append("욕설·성적")
    if h.get("music") and (h.get("source") == "song" or m.get("active_len", 0) > 3.0):
        why.append("음악")
    if o.get("drop_owner_clear") and (h.get("owner_clear") or h.get("source") in BAD_SOURCE):
        why.append("권리자 분명")
    if h.get("earrape"):
        why.append("일부러 찢은 소리")
    if h.get("speech") == "sentence":
        why.append("문장 대사")
    if int(h.get("quality") or 0) < int(o["min_quality"]):
        why.append("품질")
    if float(m.get("clip_ratio", 0)) > float(o["max_clip_ratio"]):
        why.append("찢어짐")
    return why


def score(it: dict, typ: str) -> float:
    h = it.get("heard") or {}
    s = float((h.get("fit") or {}).get(typ, 0)) * 2 + float(h.get("quality") or 3)
    if h.get("speech") == "word" and typ in SHORT_TYPES:
        s -= 0.5
    return round(s, 2)


def decide(items: list[dict], types: list[str], o: dict) -> None:
    """items 마다 auto = {keep, type, why}, score 를 채운다(제자리). 사람 값(user)은 건드리지 않는다."""
    by_hash: dict[str, str] = {}
    cands: dict[str, list[tuple[float, dict]]] = {t: [] for t in types}
    # 같은 파일 사본 중에서는 이름이 짧은 것('뽁.mp3' > '뽁 (2).mp3')을 남긴다
    for it in sorted(items, key=lambda it: (len(Path(it["key"]).name), it["key"])):
        why = drop_reasons(it, o, types)
        it["named"] = it.get("name_type") in types
        hsh = it.get("hash")
        if not why and hsh:
            if hsh in by_hash:
                why = [f"같은 소리({Path(by_hash[hsh]).name})"]
            else:
                by_hash[hsh] = it["key"]
        it["auto"] = {"keep": False, "type": it["name_type"] if it["named"] else None, "why": why}
        it["score"] = None
    # 같은 밈을 다른 이름으로 다시 올린 것(파일은 다르지만 소리가 거의 같음): 이름으로 정한 것 → 품질 → 짧은 이름 순으로 하나만
    reps: list[dict] = []
    for it in sorted((it for it in items if not it["auto"]["why"]),
                     key=lambda it: (0 if it["named"] else 1, -int((it.get("heard") or {}).get("quality") or 3),
                                     len(Path(it["key"]).name), it["key"])):
        twin = next((r for r in reps if similar(r.get("measure") or {}, it.get("measure") or {})), None)
        if twin is not None:
            it["auto"]["why"] = [f"비슷한 소리({Path(twin['key']).name})"]
        else:
            reps.append(it)
    for it in items:
        if it["auto"]["why"]:
            continue
        if it["named"]:
            it["auto"] = {"keep": True, "type": it["name_type"], "why": []}
            it["score"] = score(it, it["name_type"]) if it.get("heard") else None
            continue
        m = it.get("measure") or {}
        best = None
        for t, fit in ((it.get("heard") or {}).get("fit") or {}).items():
            if t not in cands or fit < int(o["min_fit"]):
                continue
            if float(m.get("active_len", 0)) > float(per_type(o["max_active_s"], t)):
                continue
            cands[t].append((score(it, t), it))
            if best is None or score(it, t) > score(it, best):
                best = t
        it["auto"]["type"] = best
        it["auto"]["why"] = ["어울리는 종류 없음"] if best is None else ["종류마다 고른 수를 넘음"]
    # 후보가 적은 종류부터 채워 한 파일이 여러 종류를 차지하지 않게. 이름이 같은 사본은 하나만.
    taken: set[str] = {it["key"] for it in items if it["auto"]["keep"]}
    names: set[str] = set()
    named_count = {t: sum(1 for it in items if it["auto"]["keep"] and it["auto"]["type"] == t) for t in types}
    for t in sorted(types, key=lambda t: len(cands[t])):
        n = named_count.get(t, 0)
        for sc, it in sorted(cands[t], key=lambda c: (-c[0], c[1]["key"])):
            if n >= int(per_type(o["want"], t)):
                break
            nn = norm_name(it["key"])
            if it["key"] in taken or (nn and nn in names):
                continue
            it["auto"] = {"keep": True, "type": t, "why": []}
            it["score"] = sc
            taken.add(it["key"])
            if nn:
                names.add(nn)
            n += 1
    for it in items:      # 같은 이름 사본 때문에 못 들어간 것의 이유를 바로잡는다
        if not it["auto"]["keep"] and it["auto"]["why"] == ["종류마다 고른 수를 넘음"] and norm_name(it["key"]) in names:
            it["auto"]["why"] = ["이름이 같은 사본"]


def effective(it: dict) -> tuple[bool, str | None]:
    u = it.get("user") or {}
    a = it.get("auto") or {}
    return bool(u.get("keep", a.get("keep"))), u.get("type") or a.get("type")


# ------------------------------------------------------------------ 다듬기·꾸러미

def trim(x, typ: str, m: dict, o: dict):
    """앞 무음 떼기, 종류별 길이 상한(넘으면 끝 0.06초 페이드), 피크 −1 dBFS."""
    import numpy as np
    x = x.astype(np.float64)
    cap = float(per_type(o["cap_s"], typ))
    st = max(0.0, float(m.get("onset", 0.0)) - 0.008)
    natural = float(m.get("active_end", x.size / SR)) + 0.12
    ln = max(0.02, min(natural - st, cap, x.size / SR - st))
    cut = natural - st > cap
    y = x[int(st * SR): int((st + ln) * SR)].copy()
    if y.size == 0:
        return y, 0.0
    fi, fo = min(y.size, int(0.003 * SR)), min(y.size, int((0.06 if cut else 0.015) * SR))
    y[:fi] *= np.linspace(0, 1, fi)
    y[-fo:] *= np.linspace(1, 0, fo)
    y *= PEAK / (np.abs(y).max() + 1e-12)
    return y.astype(np.float32), round(y.size / SR, 3)


OUT_RE = re.compile(r"^[a-z_]+_\d{2}\.wav$")


def build(doc: dict, pack: Path, src: Path, cfg: dict, types: list[str]) -> dict:
    """사람 값까지 반영해 꾸러미를 다시 만든다: <종류>_NN.wav, manifest.json, _들어보기_전체.wav.
    다듬은 소리는 _cache/ 에 (파일·종류)별로 남겨 두어, 고친 뒤 다시 만들 때 원본을 다시 풀지 않는다.
    반환: {"kept": n, "groups": {종류: 개수}}."""
    import numpy as np
    import soundfile as sf
    o = opts(cfg)
    cache = pack / "_cache"
    cache.mkdir(parents=True, exist_ok=True)
    keep: dict[str, list[dict]] = {}
    for it in doc["items"]:
        k, t = effective(it)
        it.pop("out", None)
        if k and t:
            keep.setdefault(t, []).append(it)
    order = [t for t in types if t in keep] + sorted(t for t in keep if t not in types)
    items, sheet, made = [], [], set()
    for t in order:
        # 사람이 넣은 것·이름으로 정한 것·점수 높은 것 순(번호가 곧 Chooser 의 돌림 순서)
        group = sorted(keep[t], key=lambda it: (0 if (it.get("user") or {}).get("keep") else 1,
                                                0 if it.get("named") else 1, -(it.get("score") or 0), it["key"]))
        for n, it in enumerate(group, 1):
            iid = f"{t}_{n:02d}"
            cap = float(per_type(o["cap_s"], t))
            ck = cache / (hashlib.sha1(f"{it['key']}|{it.get('sig')}|{cap}".encode("utf-8")).hexdigest()[:16] + ".wav")
            if not ck.is_file():
                y, _ = trim(decode(src / it["key"]), t, it.get("measure") or {}, o)
                if y.size == 0:
                    continue
                sf.write(ck, y, SR, subtype="PCM_16")
            y, sr = sf.read(ck, dtype="float32")
            dur = round(len(y) / sr, 3)
            shutil.copyfile(ck, pack / f"{iid}.wav")
            made.add(f"{iid}.wav")
            h = it.get("heard") or {}
            rec = {"id": iid, "file": f"{iid}.wav", "type": t, "duration": dur, "source_file": str(src / it["key"]),
                   "title": Path(it["key"]).stem, "license": "unknown (내 폴더)"}
            if h.get("desc"):
                rec["desc"] = h["desc"]
            if h.get("owner_clear") or h.get("source") in BAD_SOURCE:
                rec["rights_note"] = f"권리자 분명({h.get('source')})"
            items.append(rec)
            it["out"] = {"id": iid, "duration": dur}
            sheet += [y, np.zeros(int(0.45 * sr), dtype=np.float32)]
        sheet.append(np.zeros(int(0.9 * SR), dtype=np.float32))
    # 영상 시작 '탁'은 가장 짧은 hit, 첫 컷 '톡'은 첫 pop — 그 자리에서 먼저 쓴다(sfx._Chooser 의 roles)
    hits = [r for r in items if r["type"] == "hit"]
    if hits:
        min(hits, key=lambda r: r["duration"])["roles"] = ["hook_open", "cut"]
    pops = [r for r in items if r["type"] == "pop"]
    if pops:
        pops[0]["roles"] = ["hook_first_cut", "cut"]
    for f in pack.iterdir():      # 예전에 만든 소리 중 이번에 없는 것(이 꾸러미가 만든 이름만)
        if f.is_file() and OUT_RE.match(f.name) and f.name not in made:
            f.unlink()
    tmp = pack / "manifest.json.tmp"
    tmp.write_text(json.dumps({"version": 1, "made_by": "recap_shorts.fx.analyze_sfx", "source_dir": str(src),
                               "items": items}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, pack / "manifest.json")
    sh = pack / "_들어보기_전체.wav"
    if sheet and any(s.size for s in sheet):
        sf.write(sh, np.concatenate(sheet), SR, subtype="PCM_16")
    elif sh.is_file():
        sh.unlink()
    groups: dict[str, int] = {}
    for r in items:
        groups[r["type"]] = groups.get(r["type"], 0) + 1
    return {"kept": len(items), "groups": groups}
