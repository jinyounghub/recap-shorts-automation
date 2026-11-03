"""캡처 속 '이 문장이 말하는 부분'과 '가려야 할 부분'을 비전 모델로 찾아 카드로 자른다 (캡처 위주 배치).

- 문장의 image_hint 에 적힌 "캡처: 영수증 합계 부분" 같은 말을 질문으로 보낸다.
- 상자는 Codex 관례 [ymin, xmin, ymax, xmax] (0~1000 정규화)로 받아 원본 픽셀로 바꾼다.
- 개인정보(아이디·닉네임·프로필 사진·얼굴·영수증/등록 번호·전화번호)와 욕설 낱말(줄 전체가 아니라 그 낱말만)은 같은 호출에서 상자로 받아 모자이크한다.
  모델이 놓칠 수 있으므로 카드에는 needs_review 를 남긴다(사람이 '가림 편집'으로 더할 수 있음).
- 찾은 줄이 너무 가늘면 위아래를 조금 더 보여 주고, 원래 줄은 빨간 테두리로 표시한다.
- 장면을 세로로 이어 붙인 '칸 쌓기' 그림은 `pick_panels` 가 칸마다 썸네일을 한 번에 보내 문장별로 쓸 칸을 고른다.
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image

from recap_shorts.render.cards import make_card

INSTRUCTION = """\
이 그림은 인터넷 커뮤니티 글에 첨부된 캡처다(가로 {w}px, 세로 {h}px).

1) 아래 [찾을 부분] 각 항목이 그림의 어디에 있는지 상자로 찾아라.
   - 항목이 말하는 줄·댓글·단락 전체를 담되 옆 내용은 최소로. 숫자 줄이면 항목 이름과 숫자가 같이 들어가게.
   - 댓글 하나면 그 댓글의 아이디 줄부터 본문 마지막 줄까지(좋아요 줄은 빼도 됨).
   - 그림에 없으면 box 를 null 로.
2) 가려야 할 부분을 모두 상자로 찾아라: 계정 아이디·닉네임 글자, 프로필 사진(동그란 작은 사진), 사람 얼굴,
   영수증·등록·환자 번호 같은 일련번호, 전화번호, 욕설 낱말. 하나하나 따로.
   - 욕설은 그 낱말만(예: '새끼', 'ㅅㅂ', '좆같은') 상자로 — 줄 전체를 가리면 글 내용을 읽을 수 없다.

상자는 [ymin, xmin, ymax, xmax] 이고 각 값은 0~1000 으로 정규화한다.
JSON 객체 하나만 출력:
{{"parts": [{{"q": 번호, "box": [ymin, xmin, ymax, xmax]}}, ...],
  "private": [{{"what": "아이디|프로필 사진|얼굴|번호|욕설", "box": [ymin, xmin, ymax, xmax]}}, ...]}}
못 찾은 항목도 키를 빼지 말고 {{"q": 3, "box": null}} 처럼 적는다.

[찾을 부분]
{items}"""

TALL, PIECE = 1.6, 1.0      # 높이/너비가 TALL 넘으면 높이 ≈ PIECE*너비 조각으로 나눠 찾는다
MIN_ASPECT_H = 1 / 3.2     # 자른 상자의 높이/너비가 이보다 작으면(너무 가늘면) 위아래를 넓힌다
PAD = 0.02                  # 상자 둘레 여백(그림 크기 비율)
FOCUS_UPSCALE = 2.6         # 잘라 낸 카드는 작으니 조금 더 키움
FULL_WIDTH = 0.45           # 찾은 상자가 그림 너비의 이 비율 이상이면 좌우를 끝까지 보여 줌(위아래 줄 글자가 잘리지 않게)
INK = 110                   # 글자(진한 색) 판정 밝기 — 회색 시각·흐린 닉네임은 빼고 본문 글자만
WRAP_WIDTH = 560            # 댓글 띠의 한 줄 최대 폭(캡처 픽셀) — 넘으면 빈칸에서 줄을 바꿔 글자를 크게


def _px(box, w: int, h: int) -> tuple[int, int, int, int] | None:
    try:
        y0, x0, y1, x1 = [float(v) for v in box]
    except Exception:  # noqa: BLE001
        return None
    x0, x1 = sorted((max(0.0, min(1000.0, x0)), max(0.0, min(1000.0, x1))))
    y0, y1 = sorted((max(0.0, min(1000.0, y0)), max(0.0, min(1000.0, y1))))
    b = (round(x0 * w / 1000), round(y0 * h / 1000), round(x1 * w / 1000), round(y1 * h / 1000))
    return b if b[2] - b[0] >= 8 and b[3] - b[1] >= 8 else None


def parse_answer(r) -> dict:
    """모델 답(문자열 또는 dict) → dict. 자주 나오는 틀린 모양을 고친다:
    못 찾은 항목을 {"q": 4, null} 처럼 키 없이 적으면 첫 항목만 읽히고 가림이 통째로 빠진다."""
    import json
    import re
    if isinstance(r, dict):
        return r
    text = str(r or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    text = re.sub(r'("q"\s*:\s*\d+)\s*,\s*null\s*}', r'\1, "box": null}', text)
    text = re.sub(r",\s*([\]}])", r"\1", text)                       # 끝에 남은 쉼표
    try:
        out = json.loads(text)
    except ValueError:
        from recap_shorts.script.llm import parse_json_loose
        out = parse_json_loose(text)
    return out if isinstance(out, dict) else {}


def locate(path: str | Path, wants: list[str], read=None) -> dict:
    """wants: 찾을 부분 설명 목록 → {"parts": [box|None ...](wants 순서), "private": [box ...], "private_what": [종류 ...]}
    (원본 픽셀). private_what 은 private 와 같은 순서의 '아이디|프로필 사진|얼굴|번호|욕설'(모르면 "")."""
    if read is None:
        from recap_shorts.llm.codex import read_images

        def read(images, instruction):
            return read_images(images, instruction, json=False)     # 날글 → parse_answer 로 고쳐 읽음
    from recap_shorts.images.captures import split_points
    with Image.open(path) as im:
        im = im.convert("RGB")
        w, h = im.size
        n = 1 if h <= TALL * w else math.ceil(h / (PIECE * w))      # 세로로 긴 캡처(댓글 목록)는 조각마다 묻는다
        pts = split_points(im, n) if n > 1 else [0, h]
        pieces = [(im.crop((0, a, w, b)) if n > 1 else im, a, b - a) for a, b in zip(pts[:-1], pts[1:])]
    items = "\n".join(f"{k}. {t}" for k, t in enumerate(wants)) or "(없음 — parts 는 빈 목록)"
    parts: list = [None] * len(wants)
    private: list = []
    what: list = []
    for img, oy, ph in pieces:
        r = {}
        for _ in range(3):                       # 답 모양이 어긋나면(parts 목록 없음) 다시 묻는다
            try:
                r = parse_answer(read([img if n > 1 else Path(path)], INSTRUCTION.format(w=w, h=ph, items=items)))
            except Exception:  # noqa: BLE001  API 일시 오류도 다시
                r = {}
            if isinstance(r, dict) and isinstance(r.get("parts"), list):
                break
        r = r if isinstance(r, dict) else {}

        def shift(b):
            return (b[0], b[1] + oy, b[2], b[3] + oy) if b else None
        for p in r.get("parts") or []:
            try:
                k = int(p.get("q"))
            except Exception:  # noqa: BLE001
                continue
            if 0 <= k < len(wants) and p.get("box") and parts[k] is None:
                parts[k] = shift(_px(p["box"], w, ph))
        for p in r.get("private") or []:
            b = _px(p.get("box"), w, ph) if isinstance(p, dict) and p.get("box") else None
            if b:
                private.append(shift(b))
                what.append(str(p.get("what") or ""))
    return {"parts": parts, "private": private, "private_what": what, "size": [w, h]}


def overlap_ratio(a, b) -> float:
    """두 상자의 겹친 넓이 / 합친 넓이(IoU). 0~1."""
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def crop_for(box: tuple[int, int, int, int], size: tuple[int, int]) -> tuple[tuple[int, int, int, int], bool]:
    """찾은 상자 → (자를 상자, 테두리 강조 여부). 여백을 두고, 너무 가늘면 위아래를 넓힌다."""
    w, h = size
    x0, y0, x1, y1 = box
    px, py = round(PAD * w), round(PAD * h)
    if x1 - x0 >= FULL_WIDTH * w:                # 거의 한 줄 전체면 좌우를 끝까지(윗줄 글자가 잘리지 않게)
        x0, x1 = 0, w
    x0, x1 = max(0, x0 - px), min(w, x1 + px)
    y0, y1 = max(0, y0 - py), min(h, y1 + py)
    widened = False
    need = round((x1 - x0) * MIN_ASPECT_H)
    if y1 - y0 < need:
        extra = need - (y1 - y0)
        y0, y1 = y0 - extra // 2, y1 + extra - extra // 2
        if y0 < 0:
            y1, y0 = min(h, y1 - y0), 0
        if y1 > h:
            y0, y1 = max(0, y0 - (y1 - h)), h
        widened = True
    return (x0, y0, x1, y1), widened


# ---- 자르는 선·빨간 상자를 글줄 사이로 ("짤리거나 빨간색 사각박스 선이 글자에 가려서 안보임")
# 비전 모델 상자는 대충이라 그대로 자르면 위아래 줄이 반쯤 잘리고, 그대로 그리면 테두리가 글자 위를 지난다.
# 줄마다 '그 줄의 바탕(중앙값)과 확 다른 화소'를 글자로 보아 글줄을 찾고, 선을 줄 사이 빈 곳에 놓는다(밝은·어두운 바탕 모두).
LINE_DIFF = 60              # 바탕과 이만큼 다르면 글자 화소
LINE_MIN_INK = 0.002        # 줄 너비의 이 비율(최소 2화소) 이상 글자 화소가 있으면 글줄
LINE_GAP = 2                # 글자 없는 줄이 이보다 짧으면 같은 글줄(받침·점)
LINE_EDGE = 0.02            # 그림 좌우 끝 이 비율 안의 열은 안 봄 — 스크롤바·테두리가 글줄을 이어 붙이지 않게(블라인드 캡처)


def text_lines(im: Image.Image, x0: int = 0, x1: int | None = None) -> list[tuple[int, int]]:
    """[x0, x1) 열 안의 글줄 (위, 아래+1) 목록. 사진처럼 글자 화소가 끊이지 않는 곳은 키 큰 덩어리 하나로 나온다."""
    import numpy as np
    edge = max(4, round(LINE_EDGE * im.width))
    x0 = max(x0, edge)
    x1 = min(im.width if x1 is None else x1, im.width - edge)
    g = np.asarray(im.convert("L"), dtype=np.int16)[:, max(0, x0):max(x0 + 1, x1)]
    cnt = (np.abs(g - np.median(g, axis=1, keepdims=True)) > LINE_DIFF).sum(axis=1)
    rows = cnt >= max(2, round(LINE_MIN_INK * g.shape[1]))
    spans, start, blank = [], None, 0
    for i, r in enumerate(rows):
        if r:
            if start is None:
                start = i
            elif blank > LINE_GAP:
                spans.append((start, i - blank))
                start = i
            blank = 0
        elif start is not None:
            blank += 1
    if start is not None:
        spans.append((start, len(rows) - blank))
    return spans


def _line_height(lines: list[tuple[int, int]], h: int) -> float:
    hs = sorted(b - a for a, b in lines if b - a < 0.2 * h) or [max(1, h // 30)]
    return float(hs[len(hs) // 2])


def stroke_for(width: int) -> int:
    """make_card 의 빨간 테두리 두께(자른 너비 기준)."""
    return max(3, round(min(width, 900) / 160))


def snap_box_to_lines(src: str | Path | Image.Image, box: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """비전 상자 → 그 상자가 걸친 글줄(줄 높이의 35% 이상 겹친 줄)을 다 담고, 테두리가 줄 사이 빈 곳에 오게 넓힌 상자.
    글줄을 못 찾으면(사진 등) 그대로."""
    import numpy as np
    im = src if isinstance(src, Image.Image) else Image.open(src).convert("RGB")
    w, h = im.size
    x0, y0, x1, y1 = box
    lines = text_lines(im, x0, x1)
    lh = _line_height(lines, h)
    hit = [i for i, (a, b) in enumerate(lines)
           if b - a <= max(3 * lh, 0.12 * h) and min(b, y1) - max(a, y0) >= 0.35 * (b - a)]
    if not hit:
        return box
    i0, i1 = hit[0], hit[-1]
    top, bot = lines[i0][0], lines[i1][1]
    # 가로: 그 줄들의 글자 열을 낱말 사이(줄 높이 2배 미만) 빈칸까지 이어 붙여, 상자와 겹치는 덩어리만
    g = np.asarray(im.convert("L"), dtype=np.int16)[top:bot]
    cols = (np.abs(g - np.median(g, axis=1, keepdims=True)) > LINE_DIFF).sum(axis=0) > 0
    segs, s, blank = [], None, 0
    for j, c in enumerate(cols):
        if c:
            if s is None:
                s = j
            elif blank >= 2 * lh:
                segs.append((s, j - blank))
                s = j
            blank = 0
        elif s is not None:
            blank += 1
    if s is not None:
        segs.append((s, len(cols) - blank))
    segs = [(a, b) for a, b in segs if b > x0 and a < x1] or [(x0, x1)]
    left, right = min(a for a, _ in segs), max(b for _, b in segs)
    st, extra = stroke_for(w), max(2, round(0.15 * lh))
    prev_b = lines[i0 - 1][1] if i0 > 0 else 0
    next_a = lines[i1 + 1][0] if i1 + 1 < len(lines) else h
    # 테두리는 상자 안쪽으로 st 만큼 그려진다 → 위 선은 [ny0, ny0+st) 가 줄 사이 빈 곳에 오게
    ny0 = top - st - extra
    ny0 = ny0 if ny0 > prev_b else (prev_b + top - st) // 2
    ny1 = bot + st + extra
    ny1 = ny1 if ny1 < next_a else (bot + next_a + st) // 2
    return (max(0, left - st - extra), max(0, ny0), min(w, right + st + extra), min(h, ny1))


FAINT_DIFF = 8       # 상자 안 바탕색과 이만큼(RGB 한 채널) 다르면 옅은 무늬
FAINT_SHARE = 0.03   # 그런 화소가 이 비율 이상이면 빈 곳이 아니다(흐린 닉네임 약 6~7%, 줄 사이 빈 곳 0%)


def _faint_marks(im: Image.Image, box: tuple[int, int, int, int]) -> bool:
    """상자 안에 글줄로 잡히지 않는 옅은 무늬가 있는가 — 블라인드 캡처는 닉네임을 미리 흐려서 글줄 판정(LINE_DIFF)에 안 걸린다."""
    import numpy as np
    x0, y0, x1, y1 = box
    a = np.asarray(im.convert("RGB").crop((x0, y0, x1, y1)), dtype=np.int16).reshape(-1, 3)
    if not len(a):
        return False
    d = np.abs(a - np.median(a, axis=0)).max(axis=1)
    return float((d > FAINT_DIFF).mean()) >= FAINT_SHARE


def keep_off_text(src: str | Path | Image.Image, box: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """이름을 캡처 때 미리 흐린 그림(우리가 찍은 댓글·본문)의 아이디 상자: 다른 줄로 옮기지 않고,
    반도 안 걸친 진한 글줄(=흐리지 않은 본문)은 상자에서 뺀다. 이름 줄은 흐려서 글줄로 잡히지 않으므로
    snap_private 처럼 '가장 가까운 줄'로 옮기면 아래 댓글 본문 첫 줄을 가린다. 반 넘게 걸친 줄은 그대로 덮는다."""
    im = src if isinstance(src, Image.Image) else Image.open(src).convert("RGB")
    x0, y0, x1, y1 = (int(v) for v in box)
    for a, e in text_lines(im, x0, x1):
        ov = min(e, y1) - max(a, y0)
        if 0 < ov < 0.5 * (e - a):
            if a <= y0 < e:
                y0 = e
            elif a < y1 <= e:
                y1 = a
    return (x0, y0, x1, y1) if y1 - y0 >= 4 else (int(box[0]), int(box[1]), int(box[2]), int(box[3]))


def snap_private(src: str | Path | Image.Image, boxes: list) -> list[tuple[int, int, int, int]]:
    """가릴 상자(비전 모델) → 그 상자가 가리려던 글줄을 끝까지 덮게 넓힌 상자 목록.
    비전 상자가 글줄 하나만큼 위로 빗나가 닉네임이 통째로 드러나는 경우가 있다(블라인드 댓글의 '회사 · 닉네임' 줄).
    - 상자와 줄 높이의 60% 이상 겹친 글줄은 위아래 끝까지 담는다
    - 그런 줄이 없으면 가장 많이 겹친 줄 하나, 겹친 줄도 없으면(줄 사이 빈 곳) 줄 높이 안쪽 거리의 가장 가까운 줄 하나.
      단 상자 안에 옅은 무늬(캡처 때 이미 흐린 닉네임)가 있으면 빈 곳이 아니므로 그대로 둔다 — 옮기면 아래 본문 첫 줄을 가린다
    - 글줄 크기 상자(줄 높이 2.5배 이하: 닉네임·욕설 낱말)는 위아래를 그 줄에 맞춘다 — 닉네임 상자가 아래 본문 줄에
      반쯤 걸쳐 본문 첫 줄이 가려지는 경우가 있다(X 캡처). 얼굴·사진 같은 큰 상자는 넓히기만 한다
    - 좌우는 상자에 걸친 낱말(글자 사이 빈칸이 줄 높이의 1/4 미만)을 따라 넓힌다 — 옆 낱말(회사 이름)은 그대로
    사진 덩어리처럼 키 큰 '줄'은 담지 않는다. 글줄을 못 찾으면 그대로."""
    import numpy as np
    im = src if isinstance(src, Image.Image) else Image.open(src).convert("RGB")
    w, h = im.size
    g_all = np.asarray(im.convert("L"), dtype=np.int16)
    out = []
    for b in boxes:
        x0, y0, x1, y1 = (int(v) for v in b)
        lines = text_lines(im, x0, x1)
        if not lines:
            out.append((x0, y0, x1, y1))
            continue
        lh = _line_height(lines, h)
        lines = [(a, e) for a, e in lines if e - a <= max(3 * lh, 1.5 * (y1 - y0))]
        def ov(line):
            return min(line[1], y1) - max(line[0], y0)
        hit = [ln for ln in lines if ov(ln) >= 0.6 * (ln[1] - ln[0])]
        if not hit and lines and ov(max(lines, key=ov)) > 0:
            hit = [max(lines, key=ov)]
        if not hit and _faint_marks(im, (x0, y0, x1, y1)):
            out.append((x0, y0, x1, y1))     # 글줄은 없어도 옅은 무늬(캡처 때 이미 흐린 닉네임)가 있는 자리 — 옆 줄로 옮기지 않는다
            continue
        if not hit:
            near = sorted((max(a - y1, y0 - e, 0), (a, e)) for a, e in lines)
            hit = [near[0][1]] if near and near[0][0] <= lh else []
        if not hit:
            out.append((x0, y0, x1, y1))
            continue
        top, bot = min(a for a, _ in hit), max(e for _, e in hit)
        lo, hi = max(0, x0 - round(2 * lh)), min(w, x1 + round(2 * lh))
        g = g_all[top:bot, lo:hi]
        gap = g_all[max(0, top - 3):top, lo:hi] if top >= 3 else g_all[bot:bot + 3, lo:hi]   # 바탕색: 줄 바로 위(아래) 빈 줄
        bg = float(np.median(gap)) if gap.size else float(np.median(g))
        cols = (np.abs(g - bg) > LINE_DIFF).sum(axis=0) > 0
        segs, s, blank = [], None, 0
        for j, c in enumerate(cols):
            if c:
                if s is None:
                    s = j
                elif blank >= 0.25 * lh:
                    segs.append((s, j - blank))
                    s = j
                blank = 0
            elif s is not None:
                blank += 1
        if s is not None:
            segs.append((s, len(cols) - blank))
        segs = [(lo + a, lo + e) for a, e in segs if lo + e > x0 and lo + a < x1]
        pad = max(2, round(0.2 * lh))
        nx0 = min([x0] + [a for a, _ in segs]) - pad
        nx1 = max([x1] + [e for _, e in segs]) + pad
        if y1 - y0 <= 2.5 * lh:
            ny0, ny1 = top - pad, bot + pad
        else:
            ny0, ny1 = min(y0, top - pad), max(y1, bot + pad)
        out.append((max(0, nx0), max(0, ny0), min(w, nx1), min(h, ny1)))
    return out


def snap_crop(src: str | Path | Image.Image, crop: tuple[int, int, int, int],
              keep: tuple[int, int, int, int] | None = None) -> tuple[int, int, int, int]:
    """자르기 상자의 위·아래 선이 글줄 한가운데를 지나면 줄 사이로 옮긴다.
    - 반 넘게 보이는 줄은 다 담고, 덜 보이는 줄은 뺀다(keep 상자는 언제나 다 담는다).
    - 원본 그림 끝에 걸려 이미 잘린 줄(첫 줄·끝 줄이 그림 가장자리에 닿음)은 다른 줄이 남을 때 뺀다.
    - 사진처럼 줄이 안 나뉘는 곳(줄 높이 3배 넘는 덩어리)은 건드리지 않는다."""
    im = src if isinstance(src, Image.Image) else Image.open(src).convert("RGB")
    w, h = im.size
    x0, y0, x1, y1 = crop
    lines = text_lines(im, x0, x1)
    if not lines:
        return crop
    lh = _line_height(lines, h)
    pad = max(4, round(0.35 * lh))

    def text_line(i):
        a, b = lines[i]
        return b - a <= max(3 * lh, 0.12 * h)

    def above(i):          # i 번째 줄 바로 위 빈 곳 안의 자리
        prev_b = lines[i - 1][1] if i > 0 else 0
        return max(prev_b + (lines[i][0] - prev_b) // 2, lines[i][0] - pad) if i > 0 else max(0, lines[i][0] - pad)

    def below(i):          # i 번째 줄 바로 아래 빈 곳 안의 자리
        next_a = lines[i + 1][0] if i + 1 < len(lines) else h
        return min(lines[i][1] + (next_a - lines[i][1]) // 2, lines[i][1] + pad) if i + 1 < len(lines) else min(h, lines[i][1] + pad)

    inside = [i for i, (a, b) in enumerate(lines) if b > y0 and a < y1]
    if not inside:
        return crop
    # 원본에서 이미 잘린 끝 줄은 keep 에 들어 있어도 keep 에 다른 줄이 있으면 뺀다(반쪽 줄은 보기 나쁨)
    keep_n = sum(1 for a, b in lines if keep and b > keep[1] and a < keep[3])
    # 위
    i = inside[0]
    a, b = lines[i]
    if text_line(i):
        must = keep is not None and b > keep[1]                # 이 줄을 빼면 keep 이 잘림
        if a <= 1 and len(inside) > 1 and (not must or keep_n > 1):   # 원본 위 끝에서 잘린 줄
            y0 = below(i)
        elif a < y0:                                           # 자르는 선이 줄 가운데
            y0 = above(i) if must or (b - y0) >= 0.5 * (b - a) else below(i)
    # 아래
    j = inside[-1]
    a, b = lines[j]
    if text_line(j):
        must = keep is not None and a < keep[3]
        if b >= h - 1 and len(inside) > 1 and (not must or keep_n > 1):   # 원본 아래 끝에서 잘린 줄
            y1 = above(j)
        elif b > y1:
            y1 = below(j) if must or (y1 - a) >= 0.5 * (b - a) else above(j)
    if y1 - y0 < max(8, lh):
        return crop
    return (x0, max(0, y0), x1, min(h, y1))


def focus_crop(src: str | Path, box: tuple[int, int, int, int]) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int] | None]:
    """비전 상자 → (자를 상자, 빨간 테두리 상자 또는 None). 상자는 글줄에 맞추고, 자르는 선은 줄 사이로."""
    with Image.open(src) as im:
        im = im.convert("RGB")
        snapped = snap_box_to_lines(im, box)
        crop, widened = crop_for(snapped, im.size)
        crop = snap_crop(im, crop, keep=snapped)
        if snapped[1] < crop[1] or snapped[3] > crop[3]:     # 잘린 끝 줄을 뺐으면 테두리도 남은 줄에만
            inner = (box[0], max(box[1], crop[1]), box[2], min(box[3], crop[3]))
            snapped = snap_box_to_lines(im, inner) if inner[3] - inner[1] >= 8 else snapped
    return crop, (snapped if widened else None)


WORD_GAP = 9        # 낱말 사이 빈칸으로 볼 글자 없는 열 수(캡처 픽셀: 글자 사이 2~7, 낱말 사이 10 이상)
WORD_REACH = 240    # 낱말 끝을 찾아 상자 밖으로 넓히는 최대 폭(캡처 픽셀)


def _word_span(cols, lo: int, hi: int) -> tuple[int, int]:
    """한 글줄의 글자 열(cols) 에서 상자 [lo, hi) 안 글자가 낱말 중간에서 잘리면 바깥쪽 낱말 끝까지 넓힌 [a, b).
    비전 상자가 '그래서 이상한' 의 '이' 앞부분을 자르면 띠가 '기상한' 처럼 읽힌다. WORD_REACH 안에서 빈칸을 못 찾으면 그대로."""
    import numpy as np
    idx = np.nonzero(cols[lo:hi])[0]
    if not len(idx):
        return lo, hi
    first, last = lo + int(idx[0]), lo + int(idx[-1])

    def grow(x: int, step: int) -> int:
        edge, run, i = x, 0, x + step
        while 0 <= i < len(cols) and abs(i - x) <= WORD_REACH:
            if cols[i]:
                edge, run = i, 0
            else:
                run += 1
                if run >= WORD_GAP:
                    return edge
            i += step
        return edge if not (0 <= i < len(cols)) else x      # 그림 끝까지 이어진 낱말은 끝까지, 너무 길면 그대로
    return grow(first, -1), grow(last, 1) + 1


def text_strip(src: str | Path, box: tuple[int, int, int, int], private: list, out: str | Path, pad: int = 28) -> Path | None:
    """목록 속 댓글(가로로 아주 긴 상자) → 본문 글자만 남긴 띠 그림. 긴 줄은 빈칸에서 끊어 순서대로 쌓는다(줄 바꿈).
    가릴 곳은 자르기 전에 모자이크한다. 글자를 못 찾으면 None."""
    import numpy as np
    from recap_shorts.render.cards import _mosaic
    with Image.open(src) as im:
        im = im.convert("RGB")
    for b in private or []:
        _mosaic(im, tuple(b))
    x0, y0, x1, y1 = box
    bx0 = max(0, x0 - WORD_REACH)                  # 낱말 끝을 찾을 수 있게 좌우로 넓게 잘라 두고, 줄마다 상자 끝을 낱말 끝에 맞춘다
    band = im.crop((bx0, y0, min(im.width, x1 + WORD_REACH), y1))
    ink_all = np.asarray(band.convert("L")) < INK
    lo, hi = x0 - bx0, x1 - bx0
    ink = ink_all[:, lo:hi]
    xs, ys = np.nonzero(ink.sum(axis=0))[0], np.nonzero(ink.sum(axis=1))[0]
    if len(xs) == 0:
        return None
    arr = np.asarray(band)[ys[0]:ys[-1] + 1, lo + xs[0]:lo + xs[-1] + 1]
    paper = ~ink[ys[0]:ys[-1] + 1, xs[0]:xs[-1] + 1]
    bg = tuple(int(v) for v in np.median(arr[paper], axis=0)) if paper.any() else (255, 255, 255)   # 글자 둘레 바탕색
    pieces = []
    for ly0, ly1 in line_spans(ink):
        a, b = _word_span(ink_all[ly0:ly1].any(axis=0), lo, hi)
        lx = a + np.nonzero(ink_all[ly0:ly1, a:b].any(axis=0))[0]
        line = band.crop((lx[0], max(0, ly0 - 6), lx[-1] + 1, min(band.height, ly1 + 6)))
        cols = (np.asarray(line.convert("L")) < INK).sum(axis=0)
        x = 0
        while x < line.width:                    # 반으로 접기만 하면 두 줄 댓글의 순서가 섞이므로, 줄마다 끊어 쌓는다
            end = x + WRAP_WIDTH
            if end >= line.width:
                cut = line.width
            else:
                gaps = [g for g in range(end, x + int(WRAP_WIDTH * 0.55), -1) if cols[max(0, g - 4):g + 5].sum() == 0]
                cut = gaps[0] if gaps else end
            pieces.append(line.crop((x, 0, cut, line.height)))
            x = cut
            while x < line.width and cols[x] == 0:
                x += 1
    gap = 10
    w = max(p.width for p in pieces) + 2 * pad
    h = sum(p.height for p in pieces) + gap * (len(pieces) - 1) + pad
    strip = Image.new("RGB", (w, h), bg)
    y = pad // 2
    for p in pieces:
        strip.paste(p, (pad, y))
        y += p.height + gap
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    strip.save(out)
    return out


def line_spans(ink, gap: int = 4) -> list[tuple[int, int]]:
    """글자 판정 배열(행×열 bool) → 글줄마다 (위, 아래) 행. 글자 없는 행이 gap 줄 이상 이어지면 줄이 끊긴 것."""
    rows = ink.sum(axis=1) > 0
    spans, start, blank = [], None, 0
    for i, r in enumerate(rows):
        if r:
            if start is None:
                start = i
            elif blank >= gap:
                spans.append((start, i - blank))
                start = i
            blank = 0
        elif start is not None:
            blank += 1
    if start is not None:
        spans.append((start, len(rows) - blank))
    return spans


def context_of(src: str | Path, box: tuple[int, int, int, int], private: list, margin: float = 0.25) -> Image.Image:
    """글줄 띠 카드의 바탕용: 그 줄 둘레(위아래 너비의 margin 배)를 가림 처리해 잘라 준다(어차피 크게 흐려 깔림)."""
    from recap_shorts.render.cards import _mosaic
    with Image.open(src) as im:
        im = im.convert("RGB")
    for b in private or []:
        _mosaic(im, tuple(b))
    w, h = im.size
    cy, half = (box[1] + box[3]) // 2, round(w * margin)
    return im.crop((0, max(0, cy - half), w, min(h, cy + half)))


def dc_head_image(screenshot: str | Path, head_box: list[int], out: str | Path, gap: int = 60) -> Path | None:
    """디시 글 머리(제목·작성 시각 | 조회·추천·댓글 수)는 가로로 아주 길어 그대로는 글자가 작다.
    글자가 있는 왼쪽 덩어리(제목·시각)를 위에, 오른쪽 덩어리(조회·추천·댓글)를 아래에 쌓아 한 장으로 만든다(잘라 붙이기만, 새 글자 없음)."""
    import numpy as np
    with Image.open(screenshot) as im:
        head = im.convert("RGB").crop(tuple(head_box))
    g = np.asarray(head.convert("L"))
    ink = g < 200
    xs = np.nonzero(ink.sum(axis=0))[0]
    if len(xs) == 0:
        return None
    segs, s, p = [], xs[0], xs[0]
    for x in xs[1:]:
        if x - p > gap:
            segs.append((s, p))
            s = x
        p = x
    segs.append((s, p))

    def block(x0, x1):
        sub = ink[:, x0:x1 + 1]
        ys = np.nonzero(sub.sum(axis=1))[0]
        return head.crop((max(0, x0 - 12), max(0, ys[0] - 10), min(head.width, x1 + 13), min(head.height, ys[-1] + 11)))
    left = block(*segs[0])
    right = block(segs[1][0], segs[-1][1]) if len(segs) > 1 else None
    pad = 24
    w = max(left.width, right.width if right else 0) + 2 * pad
    h = left.height + (right.height + 12 if right else 0) + 2 * pad
    canvas = Image.new("RGB", (w, h), (255, 255, 255))
    canvas.paste(left, (pad, pad))
    if right:
        canvas.paste(right, (w - pad - right.width, pad + left.height + 12))
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out)
    return out


def focus_cards(src: str | Path, out_dir: str | Path, prefix: str, wants: list[tuple[int, str]],
                read=None) -> tuple[list[dict], list[tuple[int, int, int, int]]]:
    """wants: [(문장 index, 찾을 부분)] → 카드 목록 [{"file","crop","sentence","want","highlight"}], 가림 상자 목록."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    found = locate(src, [t for _, t in wants], read=read)
    blur = found["private"]
    cards = []
    for (sent, text), box in zip(wants, found["parts"]):
        if not box:
            continue
        crop, hl = focus_crop(src, box)
        out = out_dir / f"{prefix}_s{sent:02d}.png"
        info = make_card(src, out, crop=crop, blur_boxes=blur, label=None, highlight=hl, max_upscale=FOCUS_UPSCALE)
        info.update(sentence=sent, want=text, found=list(box), highlight=list(hl) if hl else None)
        cards.append(info)
    return cards, blur


PANEL_INSTRUCTION = """\
첨부된 이미지 {n}장은 한 게시글 첨부 그림에서 잘라 낸 연속된 장면 캡처다. 순서대로 0번부터 {last}번이다.
아래 [나레이션 문장] 각각에 대해, 문장이 말하는 내용을 가장 잘 *보여 주는* 장면을 최대 {per}개 골라라.
- 화면 속 글자 카드, 그래픽, 사진, 자료 화면처럼 그 요점을 직접 담은 장면을 고른다.
- 사람 얼굴이 크게 나오는 장면(사람이 말하는 장면)은 피한다 — 얼굴은 모자이크로 가려야 해서 보기 나쁘다.
  같은 내용을 다른 장면이 보여 주지 못하고, 자막이 그 문장의 핵심을 그대로 말할 때만 고른다.
- 거의 같은 장면은 겹쳐 고르지 마라. 한 장면은 한 문장에만. 맞는 장면이 없으면 빈 목록도 괜찮다.
- 고른 장면마다 실제 사람 얼굴이 보이는지(faces), 계정 아이디·닉네임이 보이는지(ids) 알려라.

JSON 객체 하나만 출력:
{{"picks": [{{"sentence": 문장번호, "panels": [장면번호, ...]}}, ...],
  "faces": {{"장면번호": true|false}}, "ids": {{"장면번호": true|false}}}}

[나레이션 문장]
{items}"""

PANEL_BATCH = 60            # 한 호출에 보낼 칸 수 상한 — 넘으면 나눠 묻고 합친다


def _as_int(v):
    try:
        return int(v)
    except Exception:  # noqa: BLE001
        return None


def _flag_map(v) -> dict[int, bool]:
    """{"3": true} 또는 [3, 5] 모양 → {3: True}"""
    out: dict[int, bool] = {}
    if isinstance(v, dict):
        for k, x in v.items():
            if _as_int(k) is not None:
                out[_as_int(k)] = bool(x) and str(x).lower() not in ("false", "0", "no")
    elif isinstance(v, list):
        for k in v:
            if _as_int(k) is not None:
                out[_as_int(k)] = True
    return out


def pick_panels(src: str | Path, boxes: list[tuple[int, int, int, int]], sentences: list[dict], read=None, *,
                max_total: int = 10, per_sentence: int = 2, thumb_w: int = 512) -> dict:
    """칸 쌓기 그림에서 문장별로 쓸 칸 고르기(Codex 비전 1회, 60칸 넘으면 나눠서).
    sentences: pipeline.sentence_infos 모양. 훅은 소개 카드가 맡으니 뺀다.
    반환 {"picks": [{"sentence", "panels": [k..]}], "faces": {k: bool}, "ids": {k: bool}} — 범위 밖·중복 제거."""
    if read is None:
        from recap_shorts.llm.codex import read_images

        def read(images, instruction):
            return read_images(images, instruction, json=False)
    sents = [s for s in sentences if s.get("role") != "hook"]
    valid = {s["index"] for s in sents}
    items = "\n".join(f"{s['index']}. {s['text']}" for s in sents)
    if not sents or not boxes:
        return {"picks": [], "faces": {}, "ids": {}}
    raw: list[tuple[int, int]] = []              # (문장, 칸) 모델이 말한 순서대로
    faces: dict[int, bool] = {}
    ids: dict[int, bool] = {}
    with Image.open(src) as im:
        im = im.convert("RGB")
        thumbs = []
        for x0, y0, x1, y1 in boxes:
            t = im.crop((x0, y0, x1, y1))
            if t.width > thumb_w:
                t = t.resize((thumb_w, max(1, round(t.height * thumb_w / t.width))), Image.LANCZOS)
            thumbs.append(t)
    for off in range(0, len(thumbs), PANEL_BATCH):
        part = thumbs[off:off + PANEL_BATCH]
        try:
            r = parse_answer(read(part, PANEL_INSTRUCTION.format(n=len(part), last=len(part) - 1, per=per_sentence, items=items)))
        except Exception:  # noqa: BLE001  한 묶음이 실패해도 나머지는 쓴다
            continue
        for p in r.get("picks") or []:
            if not isinstance(p, dict):
                continue
            si = _as_int(p.get("sentence"))
            ps = p.get("panels")
            ps = ps if isinstance(ps, list) else ([ps] if ps is not None else [])
            for k in ps:
                k = _as_int(k)
                if si in valid and k is not None and 0 <= k < len(part):
                    raw.append((si, k + off))
        faces.update({k + off: v for k, v in _flag_map(r.get("faces")).items()})
        ids.update({k + off: v for k, v in _flag_map(r.get("ids")).items()})
    # 문장마다 첫 칸부터 채우고 남는 자리에 두 번째 칸 — 모델 순서대로 채우면 max_total 이 앞 문장에서 차
    # 마지막 문장이 0칸이 되는 경우가 있다
    by_sent: dict[int, list[int]] = {}
    for si, k in raw:
        by_sent.setdefault(si, []).append(k)
    picks: dict[int, list[int]] = {}
    used: set[int] = set()
    total = 0
    # 얼굴·아이디가 보이는 칸은 뒤로, 두 번째 칸으로는 쓰지 않는다 — 모자이크 얼굴이 이어지면 보기 나쁘다
    # (칸으로 나눌 때 10칸 중 6칸이 말하는 사람 얼굴 모자이크가 되는 경우가 있다)
    def masked(k):
        return bool(faces.get(k) or ids.get(k))
    for ks in by_sent.values():
        ks.sort(key=masked)
    for rnd in range(per_sentence):
        for si, ks in by_sent.items():
            if total >= max_total or len(picks.get(si, [])) > rnd:
                continue
            k = next((k for k in ks if k not in used and (rnd == 0 or not masked(k))), None)
            if k is None:
                continue
            ks.remove(k)
            picks.setdefault(si, []).append(k)
            used.add(k)
            total += 1
    picks = {si: picks[si] for si in by_sent if si in picks}
    return {"picks": [{"sentence": si, "panels": ks} for si, ks in picks.items()],
            "faces": {k: faces.get(k, False) for k in used}, "ids": {k: ids.get(k, False) for k in used}}
