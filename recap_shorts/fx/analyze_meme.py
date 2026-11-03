"""짤 폴더 분석: 재기(크기·투명·비슷한 그림) → Codex 보기(뜻·분위기·계열·거를 것) → 고르기 → 꾸러미 (docs/library_analysis.md).

영상에서는 짤의 뜻(meaning)을 보고 문장에 맞는 짤을 고른다(fx/meme_fit.py) — 그래서 여기서 뜻을 적어 두는 게 핵심이다.
저작권은 따지지 않는다. 혐오 상징·성적·잔인한 그림은 뺀다. 실제 인물 사진은 표시만 한다.
원본 폴더는 읽기만 하고, 고른 그림은 꾸러미 폴더에 복사한다.
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import shutil
from pathlib import Path
from typing import Callable, Optional

from .analyze_sfx import batch_key, parse_answer_list
from .config import BEAT_LABELS

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
COPY_AS_IS = (".png", ".jpg", ".jpeg")
LOOK_SIDE = 512                   # Codex 에 보여 줄 크기(긴 변)

BEAT_LABEL = {"shock": "충격·놀람", "funny": "웃김·황당", "sad": "슬픔·안타까움", "angry": "분노·답답함",
              "smug": "득의양양·비웃음", "awkward": "민망·어색", "reveal": "반전·사실 공개", "question": "의문",
              "conclusion": "마무리", "neutral": "평범"}
FAMILY_LABEL = {"pepe": "페페", "apu": "아푸", "wojak": "워잭", "cat": "고양이", "dog": "강아지", "animal": "다른 동물",
                "cartoon": "만화 캐릭터", "anime": "애니", "person": "사람 사진", "text": "글씨 짤", "other": "그 밖"}

DEFAULTS = {"max_files": 400, "min_side_px": 120, "dup_distance": 12, "min_quality": 2, "max_side_px": 1600}
HASH_SIDE = 16    # 차이 해시 16×16 = 256비트. 8×8 은 같은 캐릭터의 다른 표정(페페끼리)도 같은 그림으로 봤다


def opts(cfg: dict) -> dict:
    got = ((cfg.get("analyze") or {}).get("meme") or {})
    return {**DEFAULTS, **{k: v for k, v in got.items() if not str(k).startswith("_")}}


def beats_for(cfg: dict) -> list[str]:
    """고를 수 있는 분위기 = 짤 설정 allowed_beats + 마무리(끝 짤), BEAT_LABELS 순서."""
    allowed = set(((cfg.get("memes") or {}).get("allowed_beats")) or BEAT_LABELS) | {"conclusion"}
    return [b for b in BEAT_LABELS if b in allowed]


# ------------------------------------------------------------------ 재기

def list_images(src: Path, max_files: int, skip: Path | None = None) -> tuple[list[Path], int]:
    found = []
    for pat in ("*", "*/*", "*/*/*"):
        for p in sorted(src.glob(pat)):
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS and p.name.lower() != "contact_sheet.png":
                if skip is not None:
                    try:
                        p.resolve().relative_to(skip.resolve())
                        continue
                    except ValueError:
                        pass
                found.append(p)
    return found[:max_files], max(0, len(found) - max_files)


def _open(p: Path):
    from PIL import Image
    im = Image.open(p)
    try:
        im.seek(0)          # 움직이는 그림은 첫 장면
    except EOFError:
        pass
    im.load()
    return im


def dhash(im, size: int = HASH_SIDE) -> int:
    """비슷한 그림 찾기용 차이 해시(옆 칸과의 밝기 차, size×size 비트). 줄이고 다시 압축한 사본은 256비트 중 5비트 안팎,
    같은 캐릭터의 다른 표정은 20비트 넘게 다르다."""
    import numpy as np
    from PIL import Image
    if im.mode in ("RGBA", "LA", "P"):        # 투명 부분은 흰 바탕으로 보고 잰다(같은 그림의 투명·불투명 사본이 같게)
        rgba = im.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.getchannel("A"))
        im = bg
    g = np.asarray(im.convert("L").resize((size + 1, size), Image.LANCZOS), dtype=np.int16)
    bits = 0
    for b in (g[:, :-1] > g[:, 1:]).flatten():
        bits = (bits << 1) | int(b)
    return bits


def measure(p: Path) -> dict:
    try:
        im = _open(p)
    except Exception as e:  # noqa: BLE001 — 깨진 그림
        return {"error": f"그림을 읽지 못함({type(e).__name__})"}
    w, h = im.size
    transparent = False
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        a = im.convert("RGBA").getchannel("A")
        hist = a.histogram()
        transparent = sum(hist[:16]) / max(1, w * h) > 0.05
    return {"w": w, "h": h, "transparent": transparent, "dhash": f"{dhash(im):0{HASH_SIDE * HASH_SIDE // 4}x}"}


def same_picture(m1: dict, m2: dict, max_dist: int) -> bool:
    """줄이거나 다시 압축한 사본인가: 가로세로 비율 8% 안 + 차이 해시 거리 max_dist 이하(같은 크기의 해시끼리만)."""
    h1, h2 = m1.get("dhash") or "", m2.get("dhash") or ""
    if not h1 or len(h1) != len(h2):
        return False
    r1 = int(m1.get("w", 0)) / max(1, int(m1.get("h", 1)))
    r2 = int(m2.get("w", 0)) / max(1, int(m2.get("h", 1)))
    if abs(r1 - r2) / max(r1, r2, 1e-6) > 0.08:
        return False
    # 단색·고른 그러데이션처럼 밝기 차가 거의 없는 그림은 해시가 거의 0(또는 1)이라 그림이 달라도 거리가 max_dist 안에 든다
    # (두 해시의 1 개수 합이 max_dist 이하면 내용과 상관없이 '같음') — 이런 짝은 판정하지 않는다
    bits = len(h1) * 4
    a, b = bin(int(h1, 16)).count("1"), bin(int(h2, 16)).count("1")
    if a + b <= max_dist or (bits - a) + (bits - b) <= max_dist:
        return False
    return bin(int(h1, 16) ^ int(h2, 16)).count("1") <= max_dist


# ------------------------------------------------------------------ 보기

def build_prompt(beats: list[str]) -> str:
    return (
        "아래 그림들은 한국어 커뮤니티 이슈 요약 쇼츠에 넣을 '반응 짤' 후보다. 그림마다 앞에 \"#번호\"와 파일 이름이 붙어 있다.\n"
        "짤은 내레이션 문장이 끝난 직후 1초쯤 화면을 꽉 채워, 시청자가 속으로 하는 반응을 대신 보여 준다.\n"
        "그림마다 JSON 객체 하나씩, 전부 하나의 JSON 배열로 답해라:\n"
        '{"k": 번호, "reaction": 반응 짤로 쓸 수 있으면 true(표정·몸짓·상황으로 감정이 읽힘), 풍경·문서·광고·설명 그림이면 false,\n'
        ' "name": "짧은 한국어 이름(16자 이내, 예: 물음표 띄운 개구리)",\n'
        ' "meaning": "<보이는 표정·동작> — \'<시청자 속마음 한두 마디>\' 하는 <감정>. <어떤 문장 바로 뒤에 어울리는지>." (90자 이내),\n'
        ' "beats": 분위기 1~2개(첫째가 가장 맞는 것), 다음 중에서만: ' + ", ".join(f"{b}({BEAT_LABEL[b]})" for b in beats) + ",\n"
        ' "family": ' + " | ".join(f'"{f}"' for f in FAMILY_LABEL) + " (페페 개구리=pepe, 아푸=apu),\n"
        ' "has_text": 그림 안에 읽히는 글씨가 있으면 true, "text": "그 글씨(없으면 빈 문자열)",\n'
        ' "hate": 혐오 상징(나치 문양 등)·혐오 표현이면 true, "sexual": 노출·성적이면 true, "gore": 피·상처·잔인하면 true,\n'
        ' "real_person": 실제 사람(연예인·일반인)의 사진이면 true(그림·캐릭터는 false),\n'
        ' "quality": 1~5 (흐림·워터마크·여러 장 합친 그림이면 낮게)}\n'
        "meaning 예: \"물음표를 띄우고 어깨를 으쓱 — '이게 왜?' 하는 의문·납득 안 됨. 앞뒤가 안 맞는 결정이나 이해 안 되는 이야기 바로 뒤.\"\n"
        "JSON 배열만 답해라.")


def image_b64(p: Path) -> tuple[str, str]:
    from PIL import Image
    im = _open(p)
    im = im.convert("RGBA") if im.mode in ("RGBA", "LA", "P") else im.convert("RGB")
    if im.mode == "RGBA":     # 투명 부분은 흰 바탕으로(영상의 짤 샷 배경과 같게)
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.getchannel("A"))
        im = bg
    s = min(1.0, LOOK_SIDE / max(im.size))
    if s < 1.0:
        im = im.resize((max(1, int(im.width * s)), max(1, int(im.height * s))), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=88)
    return "image/jpeg", base64.b64encode(buf.getvalue()).decode("ascii")


def clean_seen(r: dict, beats: list[str]) -> dict:
    bs = [b for b in (r.get("beats") or []) if b in beats][:2] if isinstance(r.get("beats"), list) else []
    fam = r.get("family") if r.get("family") in FAMILY_LABEL else "other"
    q = r.get("quality")
    return {"reaction": bool(r.get("reaction", True)), "name": str(r.get("name") or "").strip()[:24],
            "meaning": str(r.get("meaning") or "").strip()[:160], "beats": bs, "family": fam,
            "has_text": bool(r.get("has_text")), "text": str(r.get("text") or "")[:60],
            "hate": bool(r.get("hate")), "sexual": bool(r.get("sexual")), "gore": bool(r.get("gore")),
            "real_person": bool(r.get("real_person")),
            "quality": int(q) if isinstance(q, (int, float)) and 1 <= q <= 5 else 3}


def look_batch(batch: list[tuple[str, Path]], beats: list[str], *, model: str | None = None,
               llm: Optional[Callable[[list], object]] = None) -> dict[str, dict]:
    """[(key, 경로)] 를 한 번에 보여 주고 {key: seen}. llm 은 시험용(parts → 답)."""
    parts: list[dict] = [{"text": build_prompt(beats)}]
    for k, (key, p) in enumerate(batch, 1):
        mime, data = image_b64(p)
        parts.append({"text": f'#{k} file: "{Path(key).stem}"'})
        parts.append({"inline_data": {"mime_type": mime, "data": data}})
    if llm is not None:
        res = llm(parts)
    else:
        from recap_shorts.llm.codex import read_images

        labels = "\n".join(f'#{k}: "{Path(key).stem}"' for k, (key, _) in enumerate(batch, 1))
        prompt = build_prompt(beats) + "\nAttached images are ordered as follows:\n" + labels
        # The list parser below accepts the JSON array in this text response.
        res = read_images([p for _, p in batch], prompt, model, json=False, timeout=300)
    if isinstance(res, str):
        res = parse_answer_list(res)
    elif isinstance(res, dict):
        res = next((v for v in res.values() if isinstance(v, list)), [res])
    out: dict[str, dict] = {}
    for r in res if isinstance(res, list) else []:
        key = batch_key(r, batch)
        if key is not None:
            out[key] = clean_seen(r, beats)
    return out


# ------------------------------------------------------------------ 고르기

def name_from_file(key: str) -> str:
    """'물음표_으쓱_페페 (2).png' → '물음표 으쓱 페페'."""
    s = re.sub(r"[\s_\-]*\(?\d+\)?$", "", Path(key).stem)
    return re.sub(r"[_\-]+", " ", s).strip()[:40] or Path(key).stem


def drop_reasons(it: dict, o: dict) -> list[str]:
    m = it.get("measure") or {}
    if m.get("error"):
        return [m["error"]]
    if min(int(m.get("w", 0)), int(m.get("h", 0))) < int(o["min_side_px"]):
        return ["너무 작음"]
    s = it.get("seen")
    if s is None:
        return ["보지 못함"] if it.get("look_error") else []
    why = []
    if s.get("hate"):
        why.append("혐오 상징")
    if s.get("sexual"):
        why.append("성적")
    if s.get("gore"):
        why.append("잔인")
    if not why and not s.get("reaction", True):
        why.append("반응 짤이 아님")
    if not why and int(s.get("quality") or 0) < int(o["min_quality"]):
        why.append("품질")
    return why


def decide(items: list[dict], o: dict) -> None:
    """items 마다 auto = {keep, why} (제자리). 비슷한 그림은 큰 것 하나만 남긴다."""
    def area(it):
        m = it.get("measure") or {}
        return int(m.get("w", 0)) * int(m.get("h", 0))
    kept: list[dict] = []
    for it in sorted(items, key=lambda it: (-area(it), it["key"])):
        why = drop_reasons(it, o)
        it["dup_of"] = None
        m = it.get("measure") or {}
        if not why and m.get("dhash"):
            twin = next((k for k in kept if same_picture(k.get("measure") or {}, m, int(o["dup_distance"]))), None)
            if twin:
                why = [f"같은 그림({Path(twin['key']).name})"]
                it["dup_of"] = twin["key"]
            else:
                kept.append(it)
        it["auto"] = {"keep": not why, "why": why}


def effective(it: dict) -> dict:
    """사람 값(user)이 먼저, 없으면 본 결과, 없으면 파일 이름."""
    u = it.get("user") or {}
    s = it.get("seen") or {}
    beats = u.get("beats") if isinstance(u.get("beats"), list) else s.get("beats") or []
    return {"keep": bool(u.get("keep", (it.get("auto") or {}).get("keep"))),
            "name": u.get("name") or s.get("name") or name_from_file(it["key"]),
            "meaning": u.get("meaning") or s.get("meaning") or name_from_file(it["key"]),
            "beats": list(beats), "family": s.get("family") or "other",
            "has_text": bool(s.get("has_text")), "real_person": bool(s.get("real_person"))}


OUT_RE = re.compile(r"^m_\d{3}\.(png|jpe?g)$")


def build(doc: dict, pack: Path, src: Path, cfg: dict) -> dict:
    """고른 짤을 m_NNN.png|jpg 로 복사(움직이는 그림·webp 는 첫 장면 PNG, 너무 크면 줄임)하고 manifest.json 을 쓴다.
    반환: {"kept": n, "groups": {계열: 개수}}."""
    from PIL import Image
    o = opts(cfg)
    items, made, groups = [], set(), {}
    n = 0
    for it in sorted(doc["items"], key=lambda it: it["key"]):
        e = effective(it)
        it.pop("out", None)
        if not e["keep"]:
            continue
        n += 1
        p = src / it["key"]
        m = it.get("measure") or {}
        big = max(int(m.get("w", 0)), int(m.get("h", 0))) > int(o["max_side_px"])
        as_is = p.suffix.lower() in COPY_AS_IS and not big
        ext = (".jpg" if p.suffix.lower() == ".jpeg" else p.suffix.lower()) if as_is else ".png"
        iid = f"m_{n:03d}"
        dst = pack / f"{iid}{ext}"
        try:
            if as_is:
                shutil.copyfile(p, dst)
            else:
                im = _open(p)
                im = im.convert("RGBA") if (m.get("transparent") or im.mode in ("RGBA", "LA", "P")) else im.convert("RGB")
                s = min(1.0, int(o["max_side_px"]) / max(im.size))
                if s < 1.0:
                    im = im.resize((max(1, int(im.width * s)), max(1, int(im.height * s))), Image.LANCZOS)
                im.save(dst, "PNG")
        except Exception:  # noqa: BLE001 — 복사 못 한 그림은 꾸러미에서 뺀다
            n -= 1
            continue
        made.add(dst.name)
        rec = {"id": iid, "file": dst.name, "name": e["name"], "family": e["family"], "beats": e["beats"],
               "transparent": bool(m.get("transparent")), "size": [int(m.get("w", 0)), int(m.get("h", 0))],
               "source_file": str(p), "source_site": "", "source_url": "", "license": "unknown (내 폴더, 저작권 확인 안 함)",
               "has_text": e["has_text"], "meaning": e["meaning"]}
        if e["real_person"]:
            rec["real_person"] = True
        items.append(rec)
        it["out"] = {"id": iid}
        groups[e["family"]] = groups.get(e["family"], 0) + 1
    for f in pack.iterdir():
        if f.is_file() and OUT_RE.match(f.name) and f.name not in made:
            f.unlink()
    tmp = pack / "manifest.json.tmp"
    tmp.write_text(json.dumps({"version": 1, "made_by": "recap_shorts.fx.analyze_meme", "source_dir": str(src),
                               "items": items}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, pack / "manifest.json")
    return {"kept": len(items), "groups": groups}
