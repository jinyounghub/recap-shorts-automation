"""Portable, headless Playwright browser. Never install a browser implicitly."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path

from .http import USER_AGENT

@contextmanager
def edge_browser():
    """Compatibility name; the default is Chromium on macOS/Linux.

    An existing Chrome/Edge installation can be selected explicitly through
    RECAP_BROWSER_CHANNEL or RECAP_BROWSER_PATH, without changing user profiles.
    """
    from playwright.sync_api import sync_playwright
    channel = os.environ.get("RECAP_BROWSER_CHANNEL", "").strip()
    executable = os.environ.get("RECAP_BROWSER_PATH", "").strip()
    if channel and executable:
        raise ValueError("RECAP_BROWSER_CHANNEL 또는 RECAP_BROWSER_PATH 중 하나만 지정하세요")
    options = {"headless": True}
    if executable:
        target = Path(executable).expanduser()
        if not target.is_file():
            raise FileNotFoundError(f"기존 브라우저 실행 경로가 없습니다: {target}")
        options["executable_path"] = str(target)
    elif channel:
        options["channel"] = channel
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(**options)
        except Exception as exc:
            raise RuntimeError(
                "헤드리스 브라우저를 시작하지 못했습니다. 기존 Chrome 경로를 연결하거나 "
                "python -m playwright install chromium 으로 Chromium을 준비하세요"
            ) from exc
        try:
            yield browser
        finally:
            browser.close()


def new_context(browser, *, mobile: bool = True):
    kw = dict(user_agent=USER_AGENT + " browser", locale="ko-KR")
    if mobile:
        kw.update(viewport={"width": 430, "height": 932}, device_scale_factor=3,
                  is_mobile=True, has_touch=True)
    return browser.new_context(**kw)
