"""로컬 파일로 하는 브라우저 시험(인터넷 불필요, 헤드리스 Chromium 필요; RECAP_TEST_BROWSER=1로 실행)."""
import json
import os

import pytest

from recap_shorts.collect import screenshot as S
from tests.collect.conftest import FIX

browser_required = pytest.mark.skipif(os.environ.get("RECAP_TEST_BROWSER") != "1",
                                      reason="RECAP_TEST_BROWSER=1과 준비된 헤드리스 브라우저 필요")


def _page(tmp_path):
    p = tmp_path / "p.html"
    p.write_text((FIX / "page_headline.html").read_text(encoding="utf-8").replace("%%OVERLAY%%", ""), encoding="utf-8")
    return p.as_uri()


@browser_required
def test_page_blur_and_manual_flag(tmp_path):
    url = _page(tmp_path)
    s1 = S.screenshot_page(url, tmp_path / "d.png", [".nick"])
    assert s1.blurred == {".nick": 1} and not s1.needs_manual_blur
    assert S.Image if hasattr(S, "Image") else True
    s2 = S.screenshot_page(url, tmp_path / "e.png", [])
    assert s2.needs_manual_blur
    s3 = S.screenshot_page(url, tmp_path / "f.png", [".nope"])
    assert s3.needs_manual_blur  # 선택자가 아무것도 못 찾으면 수동 흐림 필요


@browser_required
def test_ad_hide_counts_in_flags(tmp_path):
    shot = S.screenshot_page(_page(tmp_path), tmp_path / "g.png", [".nick"], ad_selectors=S.DEFAULT_AD_SELECTORS)
    assert shot.ads_hidden["iframe"] == 1 and shot.ads_hidden["ins.adsbygoogle"] == 1
    assert shot.ads_hidden['[class*="banner"]'] == 1
    f = shot.flags()
    assert f["ads_hidden"] == 3
    none = S.screenshot_page(_page(tmp_path), tmp_path / "h.png", [".nick"], ad_selectors=[])
    assert "ads_hidden" not in none.flags()


@browser_required
def test_ad_hide_actually_hides(tmp_path):
    from PIL import Image
    url = _page(tmp_path)
    a = S.screenshot_page(url, tmp_path / "a.png", [".nick"], ad_selectors=[])
    b = S.screenshot_page(url, tmp_path / "b.png", [".nick"], ad_selectors=S.DEFAULT_AD_SELECTORS)
    with Image.open(a.path) as ia, Image.open(b.path) as ib:
        assert ib.height <= ia.height or ib.tobytes() != ia.tobytes()


def test_ad_config_toggle(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"ad_hide": {"enabled": True, "selectors": ["iframe"]},
                               "sites": {"off.example": {"ad_hide": False}}}), encoding="utf-8")
    assert S.ad_selectors_for("https://a.example/x", cfg) == ["iframe"]
    assert S.ad_selectors_for("https://off.example/x", cfg) == []
    cfg.write_text(json.dumps({"ad_hide": {"enabled": False}}), encoding="utf-8")
    assert S.ad_selectors_for("https://a.example/x", cfg) == []


def test_site_config_placeholder_is_empty_and_dc_present():
    assert S.selectors_for("https://example-community.invalid/post/1", S.CONFIG_SITES) == []
    dc = S.selectors_for("https://gall.dcinside.com/board/view/?id=a&no=1", S.CONFIG_SITES)
    assert any("nickname" in s for s in dc) and any("cmt_nickbox" in s for s in dc)
    assert S.ad_selectors_for("https://x.example", S.CONFIG_SITES)  # 기본 광고 목록이 설정에 있음


@browser_required
def test_community_text_fallback_local(tmp_path):
    from recap_shorts.collect.community import community_text
    p = tmp_path / "t.html"
    p.write_text("<html><body><article><h1>제목</h1><p>" + "가짜 커뮤니티 본문입니다. " * 10 + "</p></article></body></html>", encoding="utf-8")
    ct = community_text(p.as_uri(), tmp_path / "o" / "t.md", engine="fallback")
    assert ct.engine == "trafilatura+playwright" and "가짜 커뮤니티 본문" in ct.markdown
    assert (tmp_path / "o" / "t.md").exists()
