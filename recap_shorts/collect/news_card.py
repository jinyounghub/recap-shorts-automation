"""Draw a news card from a supplied title/outlet/date using bundled Korean fonts."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 600
BG, FG, MUTED = (24, 26, 32), (245, 245, 247), (150, 155, 168)
_FONT_DIRS = [Path(__file__).resolve().parents[2] / "assets" / "fonts"]
_REG, _BOLD = "Pretendard-Regular.otf", "Pretendard-ExtraBold.otf"


class FontNotFound(RuntimeError):
    pass


def find_font(bold: bool = False) -> Path:
    name = _BOLD if bold else _REG
    for d in _FONT_DIRS:
        p = d / name
        if p.exists():
            return p
    raise FontNotFound(f"동봉 한글 글꼴을 찾지 못했습니다: {name} — assets/fonts 폴더를 확인하세요")


def _wrap(draw, text: str, font, max_w: int, max_lines: int) -> list[str]:
    lines, cur = [], ""
    for word in text.split(" "):
        trial = (cur + " " + word).strip()
        if draw.textlength(trial, font=font) <= max_w:
            cur = trial
            continue
        if cur:
            lines.append(cur)
            cur = ""
        for ch in word:  # 한 단어가 한 줄보다 길면 글자 단위로
            if draw.textlength(cur + ch, font=font) > max_w:
                lines.append(cur)
                cur = ch
            else:
                cur += ch
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        while lines[-1] and draw.textlength(lines[-1] + "…", font=font) > max_w:
            lines[-1] = lines[-1][:-1]
        lines[-1] += "…"
    return lines


def fmt_date(d) -> str:
    return d.strftime("%Y.%m.%d") if isinstance(d, datetime) else str(d)


def draw_news_card(title: str, outlet: str, date, out_png: Path | str, source_line: bool = True) -> Path:
    """source_line=False: 아래 '출처:' 줄을 그리지 않음(출처를 영상 설명에 적을 때)."""
    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    f_out, f_title = ImageFont.truetype(str(find_font()), 34), ImageFont.truetype(str(find_font(True)), 64)
    f_date, f_src = ImageFont.truetype(str(find_font()), 30), ImageFont.truetype(str(find_font()), 28)
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    pad = 72
    d.text((pad, 56), outlet, font=f_out, fill=MUTED)
    lines = _wrap(d, title, f_title, W - 2 * pad, 3)
    y = 130
    for ln in lines:
        d.text((pad, y), ln, font=f_title, fill=FG)
        y += 88
    ds = fmt_date(date)
    d.text((pad, H - 150), ds, font=f_date, fill=MUTED)
    if source_line:
        d.line((pad, H - 96, W - pad, H - 96), fill=(60, 63, 74), width=2)
        d.text((pad, H - 78), f"출처: {outlet} ({ds})", font=f_src, fill=MUTED)
    img.save(out_png, "PNG")
    return out_png
