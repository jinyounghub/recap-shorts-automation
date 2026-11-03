"""문장 안의 그림 컷을 말이 끊기는 곳(어절 사이)에 놓는다.

그림은 문장이 끝날 때 바꾸고, 말 중간(한 낱말 안)에서는 바꾸지 않는다. 이 프로그램의 영상은 그림을 1.4초쯤마다 바꿔 한 문장에 그림이 2~3장 들어간다 —
그 장수는 그대로 두고, 문장 안 컷을 어절 경계에 놓는다. 문장 끝 컷은 건드리지 않는다(timeline.build / fx.apply 가 그대로).

어절 경계 시각을 얻는 순서
1. ASR(SenseVoice) 글자별 시각 — 문장 소리 조각에 받아쓰기를 돌리면 글자마다 들리는 시각(0.06초 간격)이 나온다.
   원문 글자와 맞춰 보면 어절 경계(= 앞 글자와 뒤 글자 시각의 가운데)를 얻는다. 못 맞춘 경계는 아래 2번으로 채운다.
   결과는 작업 폴더 voice/word_times.json 에 문장별 서명과 함께 저장해 두고 다시 쓴다(목소리를 다시 만들면 서명이 달라져 새로 돌림).
   모델이 없으면(sherpa-onnx / SenseVoice 폴더) 건너뛴다. 환경변수 RECAP_WORDCUTS_ASR=off 이거나 설정 word_cuts.asr 가 "off" 이면 안 돌림.
2. 음절 비율 — 목소리 에너지로 문장에서 실제로 말하는 구간(앞뒤 무음 제외)을 잡고, 구간을 어절 음절 수(쉼표 뒤는 쉼 몫 추가)로 나눈다.
그다음 각 경계를 목소리 에너지가 가장 낮은 곳(숨 고르기·자음 닫힘)으로 조금 옮긴다(ASR 경계 ±0.05초, 비율 경계 ±0.12초).

문장에 그림이 N장이면 경계 N-1 곳을 고른다 — 같은 간격으로 나눈 자리(목표)에 가깝고, 샷이 0.7초 이상(안 되면 0.55 → 0.4초로 낮춤),
되도록 0.8~2.2초, 붙여 읽는 말('안 / 주고', '할 / 수') 사이는 피하고, 쉼이 뚜렷한(에너지가 낮은) 곳과 쉼표 뒤를 좋아하는 비용 최소화(DP).
0.4초로도 못 고르면(어절이 N-1 곳보다 적은 아주 짧은 문장) None 을 돌려주고, 부르는 쪽이 예전처럼 똑같이 나눈다.

그림·자막은 말보다 picture_lead_s(0.2초) 먼저 바뀌게 목소리를 늦춰 붙이므로(fx/apply.py), 컷 시각은 '경계 시각 + 목소리 지연'이다.
그래야 영상에서 컷이 어절 사이에 떨어진다(지연을 빼 먹으면 컷이 앞 어절의 끝 음절 안으로 0.2초 일찍 들어간다).
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .render.captions import glued

HOP_S = 0.01
SIDECAR = "voice/word_times.json"

DEFAULTS = {
    "asr": "auto",              # auto = 모델이 있으면 돌림 · cache = 저장된 것만 · off = 안 씀
    "min_len_s": [0.7, 0.55, 0.4],   # 샷 최소 길이 단계(앞 단계로 못 고르면 다음 단계)
    "good_len_s": [0.8, 2.2],   # 되도록 이 안
    "window_asr_s": 0.05,       # ASR 경계를 에너지 골로 옮길 수 있는 범위
    "window_ratio_s": 0.12,     # 음절 비율 경계는 오차가 커서 더 넓게
    "lead_s": 0.0,              # 컷이 어절 경계보다 이만큼 먼저 바뀐다(0 = 경계에서 바로)
    "snap_meme_s": 0.4,         # 짤 시작을 가까운 어절 경계로 옮길 수 있는 범위(0 = 안 옮김)
}

SIGMA_S = 0.4          # 목표 자리에서 이만큼 벗어나면 비용 1
Q_WEIGHT = 1.0         # 경계의 상대 에너지(0 = 무음, 1 = 말하는 동안의 중앙값)에 곱하는 값
GLUE_PENALTY = 3.0     # 붙여 읽는 말 사이
COMMA_BONUS = 0.5      # 쉼표 뒤
COMMA_WEIGHT = 1.5     # 비율 추정에서 쉼표 뒤 쉼을 음절 몇 개 몫으로 칠지


def syl(word: str) -> int:
    return sum(1 for c in word if c.isalnum())


# ---------------------------------------------------------------- 목소리 에너지
@dataclass
class Energy:
    rms: np.ndarray        # 10ms 마다의 RMS(3칸 이동평균)
    hop: float = HOP_S

    def at(self, t: float) -> float:
        i = int(round(t / self.hop))
        return float(self.rms[min(max(i, 0), len(self.rms) - 1)]) if len(self.rms) else 0.0


def load_energy(path: str | Path, hop_s: float = HOP_S) -> Energy | None:
    """목소리 wav 의 10ms RMS 곡선. 못 읽으면 None."""
    try:
        try:
            import soundfile as sf
            x, sr = sf.read(str(path), dtype="float32")
        except Exception:  # noqa: BLE001
            with wave.open(str(path)) as w:
                sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
                raw = w.readframes(w.getnframes())
            if sw != 2:
                return None
            x = np.frombuffer(raw, dtype="<i2").astype("float32") / 32768.0
            if ch > 1:
                x = x.reshape(-1, ch)
        if x.ndim > 1:
            x = x.mean(axis=1)
        n = max(1, int(sr * hop_s))
        m = len(x) // n
        if m < 3:
            return None
        rms = np.sqrt((x[: m * n].reshape(m, n) ** 2).mean(axis=1))
        return Energy(np.convolve(rms, np.ones(3) / 3, mode="same"), hop_s)
    except Exception:  # noqa: BLE001
        return None


def speech_window(en: Energy | None, a: float, b: float) -> tuple[float, float, float]:
    """문장 구간 [a, b] 안에서 실제로 말하는 구간 (on, off) 과 말하는 동안의 에너지 중앙값(기준값)."""
    if en is None or len(en.rms) == 0:
        return a, b, 0.0
    i0, i1 = max(0, int(a / en.hop)), min(len(en.rms), int(b / en.hop) + 1)
    seg = en.rms[i0:i1]
    if len(seg) < 5:
        return a, b, 0.0
    hi = float(np.percentile(seg, 90))
    if hi <= 1e-4:
        return a, b, 0.0
    thr = 0.12 * hi
    idx = np.flatnonzero(seg > thr)
    if len(idx) == 0:
        return a, b, 0.0
    core = seg[idx[0]: idx[-1] + 1]
    return a + float(idx[0]) * en.hop, a + float(idx[-1] + 1) * en.hop, float(np.median(core[core > thr]))


def _snap(en: Energy, t: float, win: float, ref: float, lo: float, hi: float) -> tuple[float, float]:
    """t 근처 ±win 초에서 에너지가 가장 낮은 곳(멀수록 조금 불리). (시각, 상대 에너지)."""
    n = len(en.rms)
    c = int(round(t / en.hop))
    w = max(1, int(round(win / en.hop)))
    i0, i1 = max(int(lo / en.hop), c - w, 0), min(int(hi / en.hop), c + w, n - 1)
    if i1 < i0:
        return t, en.at(t) / ref
    idx = np.arange(i0, i1 + 1)
    cost = en.rms[idx] / ref + 0.5 * np.abs(idx - c) / w
    j = int(idx[int(np.argmin(cost))])
    return j * en.hop, float(en.rms[j] / ref)


# ---------------------------------------------------------------- 어절 경계 시각
def asr_anchors(words: list[str], tokens: list[str], ts: list[float], seg_start: float) -> dict[int, float]:
    """어절 경계 k(앞 어절 words[k-1], 뒤 어절 words[k]) -> 영상(wav) 시계 시각. 원문 글자와 ASR 글자를 맞춰
    앞 글자·뒤 글자 시각의 가운데를 쓴다. 맞춰지지 않는 경계는 빠진다."""
    chars, times = [], []
    for i, (tk, t) in enumerate(zip(tokens, ts)):
        cs = [c for c in tk if c.isalnum()]
        if not cs:
            continue
        nxt = ts[i + 1] if i + 1 < len(ts) else t + 0.15 * len(cs)
        for q, c in enumerate(cs):          # 한 토큰이 여러 글자(' 날짜')면 다음 토큰 시각까지 고르게
            chars.append(c)
            times.append(t + (nxt - t) * q / len(cs))
    if not chars:
        return {}
    spoken = "".join("".join(c for c in w if c.isalnum()) for w in words)
    sm = difflib.SequenceMatcher(None, spoken, "".join(chars), autojunk=False)
    m: dict[int, int] = {}
    for blk in sm.get_matching_blocks():
        for q in range(blk.size):
            m[blk.a + q] = blk.b + q
    out: dict[int, float] = {}
    j = 0
    for k in range(1, len(words)):
        j += syl(words[k - 1])
        if (j - 1) in m and j in m and m[j] == m[j - 1] + 1:
            out[k] = seg_start + (times[m[j - 1]] + times[m[j]]) / 2
    return out


def estimate_times(words: list[str], on: float, off: float,
                   anchors: dict[int, float] | None = None) -> tuple[dict[int, float], set[int]]:
    """어절 경계 k=1..n-1 의 시각과, 그중 anchors(ASR)에서 온 경계 번호들. anchors 에 있는 경계는 그 값(엉뚱하거나 순서가 거꾸로면 버림),
    나머지는 앞뒤 기준점 사이를 음절 수(쉼표 뒤는 쉼 몫 추가)에 비례해 나눈다."""
    n = len(words)
    w = [syl(x) + (COMMA_WEIGHT if x.rstrip("\"'”’)]").endswith(",") else 0.0) for x in words]
    tot = sum(w) or float(n)
    cum = [0.0]
    for x in w:
        cum.append(cum[-1] + x)
    ratio = {k: on + (off - on) * cum[k] / tot for k in range(1, n)}
    good: dict[int, float] = {}
    last = on - 0.1
    for k in sorted(anchors or {}):
        t = anchors[k]
        if abs(t - ratio[k]) <= 0.6 and last <= t <= off + 0.1:      # 엉뚱한 기준점·거꾸로 간 기준점은 버림
            good[k] = t
            last = t
    out: dict[int, float] = {}
    pts = [(0, on)] + sorted(good.items()) + [(n, off)]
    for (k0, t0), (k1, t1) in zip(pts, pts[1:]):
        for k in range(k0 + 1, k1):
            out[k] = t0 + (t1 - t0) * (cum[k] - cum[k0]) / ((cum[k1] - cum[k0]) or 1.0)
    out.update(good)
    return {k: out[k] for k in range(1, n)}, set(good)


@dataclass(frozen=True)
class Gap:
    k: int           # words[k-1] | words[k] 사이 (1..n-1)
    t: float         # 목소리(wav) 시계
    rel: float       # 상대 에너지 (낮을수록 또렷한 쉼). 에너지를 못 읽었으면 0.5
    glued: bool
    comma: bool
    src: str         # "asr" | "ratio"


@dataclass(frozen=True)
class Cut:
    k: int           # 고른 어절 경계 번호(그 앞 어절 수)
    t: float         # 영상 시계(= 샷 시각)
    rel: float
    glued: bool
    comma: bool
    src: str


def _len_pen(length: float, lo: float, hi: float) -> float:
    if length < lo:
        return ((lo - length) / 0.3) ** 2
    if length > hi:
        return ((length - hi) / 0.5) ** 2
    return 0.0


def _unit(c: Cut, target: float) -> float:
    v = ((c.t - target) / SIGMA_S) ** 2 + Q_WEIGHT * min(c.rel, 1.5)
    if c.glued:
        v += GLUE_PENALTY
    if c.comma:
        v -= COMMA_BONUS
    return v


def _pick(cands: list[Cut], start: float, end: float, n: int, min_len: float, good: tuple[float, float]) -> list[Cut] | None:
    """cands(시각순) 에서 n-1 곳을 골라 총비용 최소(DP). 샷 길이는 모두 min_len 이상."""
    m, INF = len(cands), float("inf")
    targets = [start + (end - start) * j / n for j in range(1, n)]
    best = [[INF] * m for _ in range(n - 1)]
    back = [[-1] * m for _ in range(n - 1)]
    for i, c in enumerate(cands):
        best[0][i] = _unit(c, targets[0]) + _len_pen(c.t - start, *good)
    for j in range(1, n - 1):
        for i, c in enumerate(cands):
            for p in range(i):
                if best[j - 1][p] == INF or c.t - cands[p].t < min_len - 1e-9:
                    continue
                v = best[j - 1][p] + _unit(c, targets[j]) + _len_pen(c.t - cands[p].t, *good)
                if v < best[j][i]:
                    best[j][i], back[j][i] = v, p
    fin = [(best[n - 2][i] + _len_pen(end - cands[i].t, *good), i) for i in range(m) if best[n - 2][i] < INF]
    if not fin:
        return None
    _, i = min(fin)
    chosen = []
    for j in range(n - 2, -1, -1):
        chosen.append(cands[i])
        i = back[j][i]
    return chosen[::-1]


# ---------------------------------------------------------------- 한 영상 분량
class WordCuts:
    """timings 문장들 + 목소리 에너지 + (있으면) ASR 글자 시각으로 문장 안 컷 자리를 고른다.

    delay_s: 영상에서 목소리가 wav 보다 늦게 나오는 초(= picture_lead_s, 그림·자막이 말보다 먼저 바뀌게 목소리를 늦춤) - cfg lead_s.
    choose/snap_meme 이 돌려주는 시각은 영상 시계(= 샷 시각)다."""

    def __init__(self, sentences: list[dict], energy: Energy | None = None, asr: dict[int, dict] | None = None,
                 delay_s: float = 0.0, cfg: dict | None = None):
        c = {**DEFAULTS, **(cfg or {})}
        self.cfg = c
        self.sents = {int(s["index"]): s for s in sentences}
        self.energy = energy
        self.asr = asr or {}
        self.delay = float(delay_s) - float(c["lead_s"])
        self.tiers = [float(x) for x in c["min_len_s"]]
        self.good = (float(c["good_len_s"][0]), float(c["good_len_s"][1]))
        self.errors: list[str] = []
        self._gaps: dict[int, list[Gap]] = {}

    # --- 어절 경계
    def words(self, index: int) -> list[str]:
        s = self.sents[index]
        return str(s.get("spoken") or s.get("text") or "").split()

    def gaps(self, index: int) -> list[Gap]:
        if index in self._gaps:
            return self._gaps[index]
        out: list[Gap] = []
        s = self.sents.get(index)
        words = self.words(index) if s else []
        if len(words) >= 2:
            a, b = float(s["start"]), float(s["end"])
            on, off, ref = speech_window(self.energy, a, b)
            tk = self.asr.get(index) or {}
            anchors = asr_anchors(words, tk.get("tokens") or [], tk.get("ts") or [], a) if tk else {}
            times, used = estimate_times(words, on, off, anchors)
            prev = on - 0.1
            for k in range(1, len(words)):
                t = times[k]
                src = "asr" if k in used else "ratio"
                rel = 0.5
                if self.energy is not None and ref > 0:
                    win = float(self.cfg["window_asr_s"] if src == "asr" else self.cfg["window_ratio_s"])
                    t, rel = _snap(self.energy, t, win, ref, on, off)
                t = max(t, prev)                       # 옮기다 순서가 뒤집히지 않게
                prev = t
                w0 = words[k - 1]
                out.append(Gap(k, round(t, 3), round(rel, 3), glued(w0, words[k]), w0.endswith(","), src))
        self._gaps[index] = out
        return out

    def cuts_in(self, index: int) -> list[Cut]:
        return [Cut(g.k, round(g.t + self.delay, 3), g.rel, g.glued, g.comma, g.src) for g in self.gaps(index)]

    # --- 문장 안 컷 고르기
    def choose(self, index: int, start: float, end: float, n: int) -> list[Cut] | None:
        """문장 구간 [start, end] 에 그림이 n장일 때 컷 n-1 곳(시각순). n < 2 이면 []. 못 고르면 None(부르는 쪽이 똑같이 나눔)."""
        if n < 2:
            return []
        try:
            cuts = self.cuts_in(index)
            if len(cuts) < n - 1:
                return None
            for min_len in self.tiers:
                valid = [c for c in cuts if start + min_len - 1e-9 <= c.t <= end - min_len + 1e-9]
                if len(valid) < n - 1:
                    continue
                res = _pick(valid, start, end, n, min_len, self.good)
                if res is not None:
                    return res
        except Exception as e:  # noqa: BLE001
            self.errors.append(f"문장 {index}: {type(e).__name__}: {e}")
        return None

    def snap_meme(self, index: int, t_old: float, lo: float, hi: float) -> Cut | None:
        """짤 시작 t_old 를 가까운 어절 경계(영상 시계 [lo, hi] 안, ±snap_meme_s)로. 없으면 None."""
        win = float(self.cfg["snap_meme_s"])
        if win <= 0 or index not in self.sents:
            return None
        try:
            near = [c for c in self.cuts_in(index) if abs(c.t - t_old) <= win and lo <= c.t <= hi]
            if not near:
                return None
            return min(near, key=lambda c: (_unit(c, t_old), c.t))
        except Exception as e:  # noqa: BLE001
            self.errors.append(f"짤 시작 문장 {index}: {type(e).__name__}: {e}")
            return None

    def text_k(self, index: int, k: int, text_words: list[str]) -> int:
        """말한 꼴(spoken) 어절 경계 k 를 자막 원문(text) 어절 경계 번호로. 어절 수가 같으면 그대로, 아니면 음절 비율로."""
        sp = self.words(index)
        if len(sp) == len(text_words):
            return k
        ws = [syl(w) for w in sp]
        frac = sum(ws[:k]) / (sum(ws) or 1)
        tw = [syl(w) for w in text_words]
        tot, acc, best = (sum(tw) or 1), 0, (9.0, 1)
        for q in range(1, len(text_words)):
            acc += tw[q - 1]
            best = min(best, (abs(acc / tot - frac), q))
        return best[1]


# ---------------------------------------------------------------- 작업 폴더에서 만들기
def _sig(s: dict, wav_size: int) -> str:
    raw = f"{s.get('start')}|{s.get('end')}|{s.get('spoken') or s.get('text')}|{wav_size}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]


def load_asr(job_dir: str | Path, sentences: list[dict], wav: Path, mode: str = "auto") -> dict[int, dict]:
    """문장 index -> {"tokens", "ts"} (ts 는 문장 시작 기준 초). 저장된 것을 쓰고, 없는 문장은 mode "auto" 에서 모델이 있으면 돌려 저장."""
    if os.environ.get("RECAP_WORDCUTS_ASR", "").strip().lower() in ("off", "0", "false", "no"):
        mode = "off"
    if mode == "off" or not wav.is_file():
        return {}
    side = Path(job_dir) / SIDECAR
    try:
        cache = json.loads(side.read_text(encoding="utf-8")) if side.is_file() else {}
    except (OSError, ValueError):
        cache = {}
    ents = cache.get("sentences") if isinstance(cache, dict) else None
    ents = ents if isinstance(ents, dict) else {}
    size = wav.stat().st_size
    got: dict[int, dict] = {}
    todo = []
    for s in sentences:
        ent = ents.get(str(s["index"]))
        if isinstance(ent, dict) and ent.get("sig") == _sig(s, size):
            got[int(s["index"])] = ent
        elif len(str(s.get("spoken") or s.get("text") or "").split()) >= 2:
            todo.append(s)
    if todo and mode == "auto":
        try:
            from .voice import asr
            if asr.available():
                import soundfile as sf
                x, sr = sf.read(str(wav), dtype="float32")
                if x.ndim > 1:
                    x = x.mean(axis=1)
                sents_doc = dict(ents)
                for s in todo:
                    seg = x[int(float(s["start"]) * sr): int(float(s["end"]) * sr)]
                    if len(seg) < sr * 0.3:
                        continue
                    r = asr.transcribe_timed(seg, sr)
                    ent = {"sig": _sig(s, size), "text": r["text"], "tokens": r["tokens"], "ts": r["ts"]}
                    got[int(s["index"])] = ent
                    sents_doc[str(s["index"])] = ent
                tmp = side.with_suffix(".tmp")
                tmp.write_text(json.dumps({"version": 1, "sentences": sents_doc}, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, side)
        except Exception:  # noqa: BLE001  모델이 없거나 폴더에 못 쓰면 음절 비율로
            pass
    return got


def for_job(job_dir: str | Path, timings: dict, cfg: dict | None, *, delay_s: float = 0.0,
            voice: str | Path = "voice/voice.wav", asr: str | None = None) -> WordCuts | None:
    """설정 timing.cuts_at_word_gaps 가 꺼져 있으면(또는 만들다 실패하면) None = 예전처럼 똑같이 나눔.
    asr 을 주면 설정 word_cuts.asr 을 그 값으로 덮는다(화면용 GET 은 "cache" — 저장된 것만 쓰고 모델은 돌리지 않음)."""
    timing = (cfg or {}).get("timing") or {}
    if not timing.get("cuts_at_word_gaps", True):
        return None
    wcfg = dict(timing.get("word_cuts") or {})
    if asr is not None:
        wcfg["asr"] = asr
    try:
        wav = Path(voice)
        if not wav.is_absolute():
            wav = Path(job_dir) / wav
        sents = timings["sentences"]
        return WordCuts(sents, load_energy(wav) if wav.is_file() else None,
                        load_asr(job_dir, sents, wav, str(wcfg.get("asr", DEFAULTS["asr"]))), delay_s, wcfg)
    except Exception:  # noqa: BLE001
        return None


def from_options(job_dir: str | Path, timings: dict, options: dict | None = None, *,
                 voice: str | Path = "voice/voice.wav", asr: str | None = None) -> WordCuts | None:
    """pipeline.write_timeline · python -m recap_shorts.timeline · 앱 화면 용: config/fx.json 과 작업 옵션 fx 로 설정을 읽고,
    fx 가 목소리를 늦출 초(picture_lead_s)를 알아서 넣는다. asr="cache" 면 저장된 받아쓰기만 쓴다(모델을 돌리지 않음)."""
    try:
        from .fx.apply import picture_lead
        from .fx.config import load_config
        ov = (options or {}).get("fx")
        cfg = load_config(None, ov if isinstance(ov, dict) else None)
        if (options or {}).get("check_asr") is False:
            asr = "off"
        return for_job(job_dir, timings, cfg, delay_s=picture_lead(cfg, options), voice=voice, asr=asr)
    except Exception:  # noqa: BLE001
        return None
