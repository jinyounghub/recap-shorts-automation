"""커뮤니티 글(사용자가 준 주소) 본문을 Markdown으로. trafilatura + 헤드리스 Playwright 렌더.

작성자·닉네임으로 보이는 줄은 지우지 않고 그대로 둔다. 결과 파일은 work/ 아래에만 쓴다.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .http import USER_AGENT, Blocked, BlockedError, HttpClient


@dataclass
class CommunityText:
    path: Path
    markdown: str
    engine: str  # trafilatura+playwright


def _fallback_markdown(url: str) -> str:
    import trafilatura
    from .browser import edge_browser, new_context
    with edge_browser() as br:
        ctx = new_context(br, mobile=False)
        try:
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            try:
                page.wait_for_load_state("networkidle", timeout=6000)
            except Exception:
                pass
            html = page.content()
        finally:
            ctx.close()
    return trafilatura.extract(html, url=url, output_format="markdown", include_comments=True,
                               favor_recall=True) or ""


def community_text(url: str, out_md: Path | str, client: HttpClient | None = None,
                   engine: str = "auto") -> CommunityText:
    """robots 확인·3초 간격 후 본문을 Markdown 파일로 저장. engine: auto | fallback"""
    out_md = Path(out_md)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    if client is not None:
        if not client.robots_allowed(url):
            raise BlockedError(Blocked(url, "robots"))
        client.wait_turn(url)
    if engine not in ("auto", "fallback"):
        raise ValueError("본문 수집 엔진은 auto 또는 fallback 입니다")
    md, used = _fallback_markdown(url), "trafilatura+playwright"
    out_md.write_text(md, encoding="utf-8")
    return CommunityText(out_md, md, used)
