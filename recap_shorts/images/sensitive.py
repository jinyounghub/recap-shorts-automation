"""자극적·잔혹한 그림에 흑백/흐림을 자동으로 입힌다 — 유튜브 광고 노란 딱지(제한적 광고)를 피하기 위해.

잔인한 그림을 흑백·흐림으로 처리해 두면 광고 제한을 받을 가능성이 줄어든다.
(아이디·얼굴·번호 가리기는 images/regions.py 가 한다. 여기는 '그림 자체가 자극적인가'를 다룬다.)

흐름
1. classify(): Codex 비전에 그림 ≤8장씩 묶어 묻는다. 장마다 level(none|mild|strong)·kind·한국어 이유.
   그림 파일 sha1 별로 `<job>/assets/sensitive.json` 에 기록해 같은 그림은 다시 묻지 않는다.
2. run(): 판정에 config/sensitive.json 의 처리(mild→gray, strong→gray_blur …)를 입힌다.
   처리 전 파일은 `<이름>.orig.png` 로 보관 → set_treatment() 가 되돌리거나 다른 처리로 바꾼다.
3. 카드(render/cards.py)에는 출처 줄·머리글이 없다(label=None) — 카드 그림 통째를 처리해도 읽어야 할 글자를 해치지 않는다.

assets.json 항목에 붙는 기록: sensitive = {level, kind, reason, treatment, sha1, note, [user], [flagged], [blocked]}
- treatment: none | gray | gray_blur | none_by_user(사람이 '원래대로'를 고름)
- flagged: strong 이라서 이 모듈이 needs_review 를 켰다는 표시(사람이 처리를 고르면 다시 끈다)

평가용 명령: python -m recap_shorts.images.sensitive <파일·폴더 ...> [--json out.json] [--no-cache]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageFilter

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "config" / "sensitive.json"
CACHE_REL = "assets/sensitive.json"

BATCH = 8                       # 한 번에 보여 주는 그림 수
SEND_SIDE = 768                 # 판정용으로 보내는 그림의 긴 변(요청 크기를 줄임 — 자극 여부는 이 정도로 보인다)
MAX_FAILS_IN_ROW = 2            # 호출이 이만큼 연달아 실패하면 나머지는 부르지 않고 건너뜀
LEVELS = ("none", "mild", "strong")
KINDS = ("blood_injury", "gore_body", "corpse_death", "accident_disaster", "violence_weapon", "animal_harm",
         "medical_procedure", "sexual_suggestive", "other")
TREATMENTS = ("none", "gray", "gray_blur")
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}

DEFAULTS = {"enabled": True, "mild": "gray", "strong": "gray_blur", "blur_sigma": 28.0, "check_ai": True}

LEVEL_KO = {"mild": "약함", "strong": "강함"}
KIND_KO = {"blood_injury": "피·부상", "gore_body": "신체 훼손", "corpse_death": "시신·사망", "accident_disaster": "사고·재난",
           "violence_weapon": "폭력·무기", "animal_harm": "동물 피해", "medical_procedure": "의료 시술",
           "sexual_suggestive": "선정적", "other": "기타"}
TREAT_KO = {"gray": "흑백 처리", "gray_blur": "흑백+흐림 처리", "none": "처리 안 함", "none_by_user": "원래대로(사람이 정함)"}

INSTRUCTION = """\
위 이미지 {n}장은 한국어 이슈 요약 쇼츠에 들어갈 화면이다(순서대로 1~{n}번). 유튜브 광고주 친화 콘텐츠 가이드라인(잔혹·폭력, 성적 암시)을
기준으로, 장마다 '그림 자체'가 얼마나 자극적인지 level 로 판정한다.

level
- none: 아래에 해당하지 않는 보통 그림. 글만 있는 게시글·댓글·메신저 캡처(글 내용이 거칠어도 none), 경기·음식·일상·인물 사진,
  만화·짤·AI 그림의 가벼운 표현. 애매하면 none.
- mild: 피·상처·부상이 눈에 보임 / 폭행·싸움·충돌 같은 폭력의 순간 / 수술·시술·상처 확대 같은 의료 장면 / 죽은 동물 /
  사고·재난 현장(시신은 안 보임) / 노출이 많거나 성적 암시가 있는 사진.
- strong: 적나라한 신체 훼손·내장·뼈 / 심한 부상 / 시신 / 사망 사고·참사 현장 / 성기·유두 노출이나 성행위 묘사 / 극단적인 학대.

규칙
- 게시글·댓글 캡처는 그림 부분(첨부 사진·영상 장면)만 본다. 글 내용(욕설·혐오·사건 설명)은 판정하지 않는다.
- 만화·게임·애니·일러스트 속 폭력은 사실적으로 보일 때만 mild/strong 으로 올린다.
- 선정성은 그림체와 상관없이 본다: 가슴·엉덩이를 과장하거나 강조한 캐릭터, 알몸·실루엣 노출, '18+'·성인 표시가 있는 표지나
  홍보 그림은 만화·게임 그림이어도 mild 다(유튜브 광고 가이드라인은 애니·게임의 성적 묘사도 같이 본다). 성기·유두 노출이나 성행위 묘사는 strong.
  (이 줄 없이 성인 게임 홍보 그림 4장 중 3장을 none 으로 놓친 적이 있다)
- 모자이크·검은 줄·흐림으로 이미 가려진 부분은 안 보이는 것으로 본다.
- kind 는 가장 두드러진 것 하나: blood_injury(피·상처·부상) | gore_body(신체 훼손·내장) | corpse_death(시신·사망) |
  accident_disaster(사고·재난) | violence_weapon(폭행·무기) | animal_harm(동물 학대·죽은 동물) | medical_procedure(의료 시술) |
  sexual_suggestive(선정적·노출) | other. level 이 none 이면 kind 는 "none".
- reason: 무엇이 보여서 그렇게 판정했는지 한국어 한 줄(40자 이내). level 이 none 이면 빈 문자열.

반드시 JSON 하나로만 답한다(모든 번호를 빠짐없이):
{"items": [{"i": 1, "level": "none", "kind": "none", "reason": ""}, {"i": 2, "level": "mild", "kind": "blood_injury", "reason": "이마에서 피가 흐름"}]}
"""
PROMPT_V = hashlib.sha1(INSTRUCTION.encode("utf-8")).hexdigest()[:10]     # 지시문을 고치면 기록이 저절로 묵은 것이 된다


# ------------------------------------------------------------------ 설정
def load_config(path: str | Path | None = None) -> dict:
    """config/sensitive.json → 기본값을 채운 설정. 파일이 없거나 깨졌으면 기본값."""
    cfg = dict(DEFAULTS)
    try:
        raw = json.loads(Path(path or CONFIG_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    if isinstance(raw, dict):
        for k in DEFAULTS:
            if k in raw:
                cfg[k] = raw[k]
    cfg["enabled"], cfg["check_ai"] = bool(cfg["enabled"]), bool(cfg["check_ai"])
    for k in ("mild", "strong"):
        if cfg[k] not in TREATMENTS:
            cfg[k] = DEFAULTS[k]
    try:
        cfg["blur_sigma"] = max(0.0, min(200.0, float(cfg["blur_sigma"])))
    except (TypeError, ValueError):
        cfg["blur_sigma"] = DEFAULTS["blur_sigma"]
    return cfg


# ------------------------------------------------------------------ 기록(그림 sha1 별)
def file_sha1(path: str | Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _model() -> str:
    from recap_shorts.llm.codex import VISION_MODEL
    return VISION_MODEL or "codex-configured"


class Cache:
    """`<job>/assets/sensitive.json`.
    items   — 처리 전 그림 sha1 → {level, kind, reason, blocked?}   (모델 답. 지시문·모델이 바뀌면 버림)
    user    — 처리 전 그림 sha1 → none|gray|gray_blur             (사람이 고른 처리 — 다시 돌려도 지킴)
    applied — 처리한 뒤 파일 sha1 → {orig, treatment, level, kind, reason}   (카드를 다시 만들지 않는 재실행에서 이미 처리된 파일을 알아봄)"""

    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path else None
        self.items: dict[str, dict] = {}
        self.user: dict[str, str] = {}
        self.applied: dict[str, dict] = {}
        self.dirty = False
        if self.path and self.path.is_file():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw = {}
            if isinstance(raw, dict):
                self.user = {k: v for k, v in (raw.get("user") or {}).items() if v in TREATMENTS}
                self.applied = {k: v for k, v in (raw.get("applied") or {}).items() if isinstance(v, dict)}
                if raw.get("prompt") == PROMPT_V and raw.get("model") == _model():
                    self.items = {k: v for k, v in (raw.get("items") or {}).items() if isinstance(v, dict)}

    def get(self, sha: str) -> dict | None:
        return self.items.get(sha)

    def put(self, sha: str, rec: dict) -> None:
        self.items[sha] = rec
        self.dirty = True

    def save(self) -> None:
        if not self.path or not self.dirty:
            return
        doc = {"version": 1, "prompt": PROMPT_V, "model": _model(), "items": self.items, "user": self.user,
               "applied": self.applied}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
            self.dirty = False
        except OSError:
            pass


# ------------------------------------------------------------------ 판정
def _prepare(path: str | Path) -> Image.Image:
    with Image.open(path) as im:
        im = im.convert("RGB")
        s = min(1.0, SEND_SIDE / max(im.size))
        if s < 1.0:
            im = im.resize((max(1, round(im.width * s)), max(1, round(im.height * s))), Image.LANCZOS)
        return im.copy()


def _is_block(exc: Exception) -> bool:
    """Explicit refusal markers from an injected classifier, never connection errors."""
    s = str(exc)
    return "blockReason" in s or any(f"finishReason={x}" in s for x in (
        "SAFETY", "PROHIBITED_CONTENT", "IMAGE_SAFETY", "IMAGE_PROHIBITED_CONTENT", "BLOCKLIST", "SPII"))


def _norm(it: dict) -> dict | None:
    level = str(it.get("level") or "").strip().lower()
    if level not in LEVELS:
        return None
    kind = str(it.get("kind") or "").strip().lower()
    if level == "none":
        kind = "none"
    elif kind not in KINDS:
        kind = "other"
    reason = " ".join(str(it.get("reason") or "").split())[:80] if level != "none" else ""
    return {"level": level, "kind": kind, "reason": reason}


def _parse(out, n: int) -> dict[int, dict]:
    """모델 답 → {1-기준 번호: {level, kind, reason}}. 번호가 없으면 순서대로(개수가 n 일 때만)."""
    items = out.get("items") if isinstance(out, dict) else out if isinstance(out, list) else None
    if not isinstance(items, list):
        return {}
    items = [it for it in items if isinstance(it, dict)]
    rows: dict[int, dict] = {}
    if items and not any("i" in it for it in items):
        if len(items) != n:
            return {}
        for pos, it in enumerate(items, 1):
            rec = _norm(it)
            if rec:
                rows[pos] = rec
        return rows
    for it in items:
        try:
            i = int(it.get("i"))
        except (TypeError, ValueError):
            continue
        rec = _norm(it)
        if rec and 1 <= i <= n and i not in rows:
            rows[i] = rec
    return rows


def classify(paths: list, read_fn, cache: Cache | None = None, *, batch: int = BATCH) -> dict:
    """그림마다 {level, kind, reason, sha1, cached} 또는 {error, [sha1]}.
    read_fn(images, instruction) -> 모델 답(JSON 객체). 돌려주는 값: {"results": [...], "calls", "cached", "errors": [...]}.
    같은 파일 내용은 한 번만 묻고, 기록(cache)에 있으면 묻지 않는다. 모델이 안전 문제로 답을 막은 그림은 strong/other 로 본다."""
    batch = max(1, min(int(batch), BATCH))
    n = len(paths)
    results: list[dict | None] = [None] * n
    stat = {"calls": 0, "cached": 0, "fails": 0}
    groups: dict[str, list[int]] = {}
    for k, p in enumerate(paths):
        try:
            sha = file_sha1(p)
        except OSError as e:
            results[k] = {"error": f"파일을 읽지 못함: {type(e).__name__}"}
            continue
        hit = cache.get(sha) if cache else None
        if hit:
            results[k] = {**hit, "sha1": sha, "cached": True}
            stat["cached"] += 1
        else:
            groups.setdefault(sha, []).append(k)
    heads = [(sha, ks) for sha, ks in groups.items()]            # 같은 내용은 첫 그림만 보냄

    def done(sha: str, ks: list[int], rec: dict, store: bool = True) -> None:
        if store and cache is not None:
            cache.put(sha, rec)
        for j, k in enumerate(ks):
            results[k] = {**rec, "sha1": sha, "cached": j > 0}

    def fail(sha: str, ks: list[int], msg: str) -> None:
        for k in ks:
            results[k] = {"error": msg, "sha1": sha}

    def ask(chunk: list[tuple[str, list[int]]]) -> None:
        if stat["fails"] >= MAX_FAILS_IN_ROW:
            for sha, ks in chunk:
                fail(sha, ks, "앞선 호출이 계속 실패해 건너뜀")
            return
        imgs, sent = [], []
        for sha, ks in chunk:
            try:
                imgs.append(_prepare(paths[ks[0]]))
                sent.append((sha, ks))
            except Exception as e:  # noqa: BLE001
                fail(sha, ks, f"그림을 열지 못함: {type(e).__name__}")
        if not sent:
            return
        stat["calls"] += 1
        try:
            out = read_fn(imgs, INSTRUCTION.replace("{n}", str(len(imgs))))
        except Exception as e:  # noqa: BLE001
            if _is_block(e):
                if len(sent) > 1:                                  # 어느 그림이 막혔는지 알려고 한 장씩 다시 묻는다
                    for one in sent:
                        ask([one])
                    return
                sha, ks = sent[0]
                done(sha, ks, {"level": "strong", "kind": "other", "reason": "모델이 안전 문제로 답을 막음", "blocked": True})
                stat["fails"] = 0
                return
            stat["fails"] += 1
            for sha, ks in sent:
                fail(sha, ks, f"{type(e).__name__}: {str(e)[:120]}")
            return
        stat["fails"] = 0
        rows = _parse(out, len(sent))
        for pos, (sha, ks) in enumerate(sent, 1):
            if pos in rows:
                done(sha, ks, rows[pos])
            else:
                fail(sha, ks, "모델 답에 이 그림이 빠짐")

    for a in range(0, len(heads), batch):
        ask(heads[a:a + batch])
    errors = [r["error"] for r in results if r and r.get("error")]
    return {"results": results, "calls": stat["calls"], "cached": stat["cached"], "errors": errors}


# ------------------------------------------------------------------ 처리(흑백·흐림)
def orig_path(path: str | Path) -> Path:
    p = Path(path)
    return p.with_name(p.stem + ".orig.png")


def treat_image(im: Image.Image, treatment: str, sigma: float) -> Image.Image:
    if treatment not in ("gray", "gray_blur"):
        return im
    g = im.convert("L").convert("RGB")
    if treatment == "gray_blur" and sigma > 0:
        g = g.filter(ImageFilter.GaussianBlur(sigma * g.width / 1080))   # 카드(1080 폭) 기준 sigma — 더 큰 그림은 비례해서
    return g


def _write_treated(path: Path, src: Path, treatment: str, sigma: float) -> None:
    with Image.open(src) as im:
        out = treat_image(im.convert("RGB"), treatment, sigma)
    fmt = "JPEG" if path.suffix.lower() in (".jpg", ".jpeg") else "PNG"
    tmp = path.with_name(path.name + ".tmp")
    out.save(tmp, format=fmt)
    os.replace(tmp, path)


def note_text(sens: dict) -> str:
    """review_reason 에 덧붙는 한 줄."""
    level, treatment = sens.get("level") or "none", sens.get("treatment") or "none"
    if level == "none" and treatment in ("none", "none_by_user"):
        return ""
    label = f"자극적인 그림({LEVEL_KO.get(level, level)}·{KIND_KO.get(sens.get('kind'), '기타')})" if level != "none" else "그림"
    text = f"{label} — {TREAT_KO.get(treatment, treatment)}"
    if sens.get("user") and treatment in ("gray", "gray_blur"):
        text += "(사람이 정함)"
    return text + (f": {sens['reason']}" if sens.get("reason") else "")


def _join(a: str | None, b: str) -> str | None:
    if not b:
        return a
    return f"{a} / {b}" if a else b


def _swap_note(reason: str | None, old: str | None, new: str) -> str | None:
    """review_reason 에서 지난 처리 문구(old)를 빼고 새 문구(new)를 덧붙인다."""
    if old and reason:
        parts = [x for x in reason.split(" / ") if x != old]
        reason = " / ".join(parts) or None
    return _join(reason, new)


def _record(level: str, kind: str, reason: str, treatment: str, sha: str, **extra) -> dict:
    sens = {"level": level, "kind": kind, "reason": reason, "treatment": treatment, "sha1": sha}
    sens.update({k: v for k, v in extra.items() if v})
    sens["note"] = note_text(sens)
    return sens


def _note_in(e: dict, sens: dict) -> None:
    note = sens.get("note")
    if note and note not in (e.get("review_reason") or ""):
        e["review_reason"] = _join(e.get("review_reason"), note)


def apply_new(job_dir: str | Path, e: dict, treatment: str, sigma: float) -> str:
    """e['file'](방금 만든 처리 전 그림)을 .orig.png 로 보관하고 처리한 그림으로 바꾼다. 처리한 파일의 sha1 을 돌려준다."""
    p = Path(job_dir) / e["file"]
    shutil.copyfile(p, orig_path(p))
    _write_treated(p, orig_path(p), treatment, sigma)
    return file_sha1(p)


def _adopt(e: dict, ap: dict, cache: Cache) -> None:
    """이미 처리돼 있는 파일(예: 대상 사진 캐시로 카드를 다시 만들지 않은 재실행)의 항목 기록을 되살린다."""
    user = ap.get("orig") in cache.user
    sens = _record(ap.get("level") or "none", ap.get("kind") or "none", ap.get("reason") or "", ap.get("treatment") or "gray",
                   ap.get("orig") or "", user=user)
    if sens["level"] == "strong" and not user and not e.get("needs_review"):
        sens["flagged"] = True
        e["needs_review"] = True
    e["sensitive"] = sens
    _note_in(e, sens)


def run(job_dir: str | Path, entries: list[dict], *, read_fn, cfg: dict | None = None, log=None) -> dict:
    """assets.json 항목들(제자리에서 고침)에서 아직 검사하지 않은 그림을 판정하고 처리한다.
    돌려주는 값: {"disabled"?, "checked", "treated": {"gray", "gray_blur"}, "levels": {...}, "flagged", "calls", "cached", "errors": [...]}."""
    cfg = cfg or load_config()
    res = {"checked": 0, "treated": {"gray": 0, "gray_blur": 0}, "levels": {"none": 0, "mild": 0, "strong": 0},
           "flagged": 0, "calls": 0, "cached": 0, "errors": []}
    if not cfg["enabled"]:
        res["disabled"] = True
        return res
    job_dir = Path(job_dir)
    cache = Cache(job_dir / CACHE_REL)
    todo: list[tuple[dict, Path]] = []
    for e in entries:
        if e.get("sensitive") or e.get("status") not in ("ok", "reused") or e.get("role") == "dc_head":
            continue
        if e.get("kind") == "ai" and not cfg["check_ai"]:
            continue
        p = job_dir / (e.get("file") or "")
        if not p.is_file():
            continue
        try:
            ap = cache.applied.get(file_sha1(p))
        except OSError:
            continue
        if ap and orig_path(p).is_file():
            _adopt(e, ap, cache)
            continue
        todo.append((e, p))
    rep = classify([p for _, p in todo], read_fn, cache)
    res["calls"], res["cached"], res["errors"] = rep["calls"], rep["cached"], list(rep["errors"])
    for (e, p), r in zip(todo, rep["results"]):
        if r is None or r.get("error"):
            continue
        try:
            sha, level = r["sha1"], r["level"]
            user = cache.user.get(sha)
            if user is not None:
                treatment = user if user != "none" else "none_by_user"
            else:
                treatment = cfg.get(level, "none") if level in ("mild", "strong") else "none"
            sens = _record(level, r["kind"], r["reason"], treatment, sha, user=user is not None, blocked=r.get("blocked"))
            if treatment in ("gray", "gray_blur"):
                out_sha = apply_new(job_dir, e, treatment, cfg["blur_sigma"])
                cache.applied[out_sha] = {"orig": sha, "treatment": treatment, "level": level, "kind": r["kind"],
                                          "reason": r["reason"]}
                cache.dirty = True
                res["treated"][treatment] += 1
            if level == "strong" and user is None:
                if not e.get("needs_review"):
                    sens["flagged"] = True
                e["needs_review"] = True
                res["flagged"] += 1
            e["sensitive"] = sens
            _note_in(e, sens)
            res["checked"] += 1
            res["levels"][level] += 1
        except Exception as ex:  # noqa: BLE001
            res["errors"].append(f"{Path(e.get('file', '?')).name}: {type(ex).__name__} {str(ex)[:100]}")
            if log:
                log(f"민감한 그림 처리 실패 {e.get('file')}: {type(ex).__name__} {str(ex)[:100]}")
    cache.save()
    return res


# ------------------------------------------------------------------ 사람이 고름·되돌림
def set_treatment(job_dir: str | Path, e: dict, choice: str, cfg: dict | None = None) -> dict:
    """사람이 한 그림의 처리를 고른다: none(원래대로) | gray(흑백만) | gray_blur(흑백+흐림). e 를 제자리에서 고치고 sensitive 를 돌려준다.
    처리는 항상 처리 전 그림(.orig.png)에서 다시 만든다. 고른 값은 기록에 남겨 다시 돌려도 지킨다."""
    if choice not in TREATMENTS:
        raise ValueError(f"처리는 {'|'.join(TREATMENTS)} 중 하나여야 합니다")
    cfg = cfg or load_config()
    job_dir = Path(job_dir)
    p = job_dir / e["file"]
    o = orig_path(p)
    sens = dict(e.get("sensitive") or {})
    treated = sens.get("treatment") in ("gray", "gray_blur")
    if not p.is_file():
        raise FileNotFoundError("그림 파일이 없습니다")
    if treated and not o.is_file():
        raise FileNotFoundError("처리 전 그림(.orig.png)이 없어 되돌리거나 바꿀 수 없습니다")
    sha = sens.get("sha1") or file_sha1(o if treated else p)
    cache = Cache(job_dir / CACHE_REL)
    if choice == "none":
        if treated:
            shutil.copyfile(o, p)
        treatment = "none_by_user"
    else:
        if not treated:
            shutil.copyfile(p, o)                       # 지금 파일이 처리 전 그림
        _write_treated(p, o, choice, cfg["blur_sigma"])
        treatment = choice
    cache.user[sha] = choice
    if choice != "none":
        cache.applied[file_sha1(p)] = {"orig": sha, "treatment": choice, "level": sens.get("level") or "none",
                                       "kind": sens.get("kind") or "none", "reason": sens.get("reason") or ""}
    cache.dirty = True
    cache.save()
    old_note = sens.get("note")
    flagged = sens.pop("flagged", False)
    sens.update(level=sens.get("level") or "none", kind=sens.get("kind") or "none", reason=sens.get("reason") or "",
                treatment=treatment, sha1=sha, user=True)
    sens["note"] = note_text(sens)
    e["review_reason"] = _swap_note(e.get("review_reason"), old_note, sens["note"])
    if flagged:                                         # 이 모듈이 켠 확인 표시는 사람이 정했으니 끈다
        e["needs_review"] = False
    e["sensitive"] = sens
    return sens


def after_remake(job_dir: str | Path, e: dict, cfg: dict | None = None) -> None:
    """카드를 처리 전 그림으로 다시 만든 직후(가리기 등) 부른다 — 처리를 새 카드에 다시 입히고 안내 문구를 지킨다."""
    sens = e.get("sensitive")
    if not sens:
        return
    job_dir = Path(job_dir)
    if sens.get("treatment") in ("gray", "gray_blur"):
        cfg = cfg or load_config()
        out_sha = apply_new(job_dir, e, sens["treatment"], cfg["blur_sigma"])
        new_orig = file_sha1(orig_path(job_dir / e["file"]))
        cache = Cache(job_dir / CACHE_REL)
        if sens.get("sha1") in cache.user:
            cache.user[new_orig] = cache.user[sens["sha1"]]
        cache.applied[out_sha] = {"orig": new_orig, "treatment": sens["treatment"], "level": sens.get("level") or "none",
                                  "kind": sens.get("kind") or "none", "reason": sens.get("reason") or ""}
        cache.dirty = True
        cache.save()
        sens["sha1"] = new_orig
    sens.pop("flagged", None)                           # 가리기는 needs_review 를 끈다 — 사람이 본 것
    _note_in(e, sens)


def forget(job_dir: str | Path, e: dict) -> None:
    """그림이 새로 만들어져(AI 이미지 다시 만들기) 낡은 처리 기록·.orig.png 를 지운다."""
    sens = e.pop("sensitive", None)
    p = Path(job_dir) / e["file"]
    orig_path(p).unlink(missing_ok=True)
    if sens:
        e["review_reason"] = _swap_note(e.get("review_reason"), sens.get("note"), "")
        if sens.get("flagged"):
            e["needs_review"] = False


# ------------------------------------------------------------------ 평가용 명령
def _expand(args: list[str]) -> list[Path]:
    out: list[Path] = []
    for a in args:
        p = Path(a)
        if p.is_dir():
            out += sorted(q for q in p.rglob("*") if q.is_file() and q.suffix.lower() in IMAGE_EXT)
        elif p.is_file():
            out.append(p)
        else:
            print(f"없는 경로: {a}", file=sys.stderr)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m recap_shorts.images.sensitive",
                                 description="그림의 자극·잔혹 정도를 Codex 비전으로 판정해 보여 준다(그림은 고치지 않는다).")
    ap.add_argument("paths", nargs="+", help="그림 파일 또는 폴더(폴더는 하위까지)")
    ap.add_argument("--json", dest="json_out", help="결과를 이 JSON 파일에도 쓴다")
    ap.add_argument("--no-cache", action="store_true", help="기록을 읽지도 쓰지도 않고 모두 새로 묻는다")
    ap.add_argument("--cache", help="기록 파일 경로(기본: 임시 폴더의 recap_sensitive_cli_cache.json)")
    ap.add_argument("--batch", type=int, default=BATCH, help=f"한 호출에 보낼 그림 수(최대 {BATCH})")
    args = ap.parse_args(argv)
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    paths = _expand(args.paths)
    if not paths:
        print("판정할 그림이 없습니다", file=sys.stderr)
        return 2
    from recap_shorts.llm.codex import read_images

    def read_fn(imgs, instr):
        return read_images(imgs, instr, json=True, thinking="low")

    cache = None if args.no_cache else Cache(args.cache or Path(tempfile.gettempdir()) / "recap_sensitive_cli_cache.json")
    rep = classify(paths, read_fn, cache, batch=args.batch)
    if cache:
        cache.save()
    rows, counts = [], {"none": 0, "mild": 0, "strong": 0, "error": 0}
    for p, r in zip(paths, rep["results"]):
        r = r or {"error": "결과 없음"}
        row = {"file": str(p), "level": r.get("level"), "kind": r.get("kind"), "reason": r.get("reason", ""),
               "cached": bool(r.get("cached")), "error": r.get("error")}
        if r.get("blocked"):
            row["blocked"] = True
        rows.append(row)
        counts[row["level"] if row["level"] in LEVELS else "error"] += 1
        if row["error"]:
            print(f"[오류   ] {p}  {row['error']}")
        else:
            print(f"[{row['level']:<7}] {row['kind']:<18} {row['reason']}  — {p}" + ("  (기록)" if row["cached"] else ""))
    print(f"\n{len(paths)}장 · none {counts['none']} · mild {counts['mild']} · strong {counts['strong']} · 오류 {counts['error']}"
          f" · Codex {rep['calls']}회 · 기록에서 {rep['cached']}장")
    if args.json_out:
        Path(args.json_out).write_text(json.dumps({"model": _model(), "prompt": PROMPT_V, "counts": counts,
                                                   "calls": rep["calls"], "items": rows}, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    return 1 if counts["error"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
