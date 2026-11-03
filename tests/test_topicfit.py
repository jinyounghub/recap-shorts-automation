"""채널 대주제: 설정·맞춤 점수·순서·보관·CLI. 진짜 Codex 는 부르지 않는다(가짜 LLM)."""
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from recap_shorts.collect import dcbest as B
from recap_shorts.collect import topicfit as T

KST = timezone(timedelta(hours=9))
NOW = datetime(2026, 10, 18, 12, 30, tzinfo=KST)
FIX = Path(__file__).parent / "fixtures" / "dcbest"
REAL_CONFIG = Path(__file__).resolve().parents[1] / "config" / "channel.json"

PRESETS = [{"id": "daily_drama", "label": "일상 논란·썰", "desc": "일상 갈등·썰"},
           {"id": "money_life", "label": "돈·생활 경제", "desc": "돈과 생활 경제"}]
# 조회수 순: 1006 1005 1004 1003 1001 1002
FIT = {1006: 0, 1005: 2, 1004: 1, 1003: 2, 1001: 0, 1002: 1}
LINE = re.compile(r"^(\d+)\. (?:\[[^\]]*\] )?(.*)$")


class FakeFitLLM:
    """프롬프트의 '번호. [태그] 제목' 줄을 읽어 FIT 표대로 답한다(실제 모델처럼 번호를 키로)."""

    def __init__(self, items, table=None):
        self.title2no = {i.title: i.no for i in items}
        self.table = dict(FIT if table is None else table)
        self.prompts: list[str] = []
        self.models: list[str] = []
        self.fail_on: set[int] = set()       # 이 번째(1부터) 호출은 예외

    def __call__(self, prompt, model=None):
        self.prompts.append(prompt)
        self.models.append(model)
        if len(self.prompts) in self.fail_on:
            raise RuntimeError("Codex API 실패: HTTP 429: quota")
        fits = {}
        for ln in prompt.splitlines():
            m = LINE.match(ln)
            if m and m.group(2) in self.title2no:
                fits[m.group(1)] = self.table[self.title2no[m.group(2)]]
        return {"fits": fits}


def write_cfg(d: Path, main_topic=None, custom=None, batch=30):
    (d / "channel.json").write_text(json.dumps({"main_topic": main_topic, "presets": PRESETS, "custom": custom,
                                                "fit": {"batch": batch, "model": "gemini-3.8-flash"}}, ensure_ascii=False),
                                    encoding="utf-8")


# ---------------------------------------------------------------- 설정


def test_committed_default_config_is_valid():
    cfg = T.load_config(REAL_CONFIG, REAL_CONFIG.parent / "no_such_local.json")
    assert cfg["main_topic"] == "daily_drama" and cfg["source"] == "default"
    assert [p["id"] for p in cfg["presets"]] == ["daily_drama", "money_life", "curious_facts"]
    assert all(p["label"] and p["desc"] for p in cfg["presets"])
    t = T.resolve(cfg)
    assert t and t.label == "일상 논란·썰" and t.key.startswith("daily_drama-")
    assert cfg["fit"]["batch"] >= 1 and cfg["fit"]["model"] is None


def test_missing_or_broken_config_means_none(tmp_path):
    bad = tmp_path / "x.json"
    bad.write_text("{not json", encoding="utf-8")
    for p in (tmp_path / "nope.json", bad):
        cfg = T.load_config(p, tmp_path / "nope2.json")
        assert cfg["main_topic"] is None and T.resolve(cfg) is None


def test_local_topic_settings_preserve_custom_text_and_drop_legacy_model(tmp_path):
    write_cfg(tmp_path)
    base, local = tmp_path / "channel.json", tmp_path / "channel.local.json"
    cfg = T.save_local("custom", "  요즘   뜨는 밈 ", path=base, local=local)
    assert T.resolve(cfg).desc == "요즘 뜨는 밈"
    assert cfg["fit"]["model"] is None
    T.save_local("daily_drama", path=base, local=local)
    cfg = T.save_local("custom", path=base, local=local)
    assert T.resolve(cfg).desc == "요즘 뜨는 밈"
    assert T.resolve(T.save_local(None, path=base, local=local)) is None
    with pytest.raises(ValueError):
        T.save_local("unknown-topic", path=base, local=local)


def test_partial_batch_can_resume_from_cache_without_repeating_completed_calls(tmp_path):
    items = B.sort_items(B.parse_best_list((FIX / "list_page1.html").read_text(encoding="utf-8"), NOW), "views")
    rows = [(i.no, i.tag, i.title) for i in items]
    topic = T.Topic("daily", "일상", "일상 갈등", "daily-1")
    llm = FakeFitLLM(items)
    llm.fail_on = {2}
    fits, state, _ = T.score(rows, topic, llm, tmp_path, batch=4)
    assert state == "partial" and len(fits) == 4 and len(llm.prompts) == 2
    fits, state, _ = T.score(rows, topic, llm, tmp_path, batch=4)
    assert state == "ok" and fits == FIT and len(llm.prompts) == 3
    assert llm.models == [None, None, None]
    fits, state, _ = T.score(rows, topic, llm, tmp_path, batch=4)
    assert state == "ok" and len(llm.prompts) == 3


def test_no_topic_makes_no_llm_call(tmp_path):
    items = [{"no": 3, "title": "셋"}, {"no": 1, "title": "하나"}]
    out, extra = T.apply_to_views(items, None, lambda *a: pytest.fail("unexpected Codex call"), tmp_path)
    assert out == items and extra["fit"] == "off"


# ---------------------------------------------------------------- 단위
def test_parse_answer_shapes():
    assert T.parse_answer({"fits": {"1": 2, "2": 0, "3": 1}}, 3) == {1: 2, 2: 0, 3: 1}
    assert T.parse_answer({"1": 2, "2": "1"}, 3) == {1: 2, 2: 1}
    assert T.parse_answer({"fits": [{"i": 1, "fit": 2}, {"id": 3, "score": 0}]}, 3) == {1: 2, 3: 0}
    assert T.parse_answer({"fits": {"0": 2, "4": 2, "1": 3, "2": 1.5, "3": True, "x": 1}}, 3) == {}
    assert T.parse_answer(["nope"], 3) == {} and T.parse_answer({"fits": "?"}, 3) == {}


def test_order_is_stable_and_unscored_between():
    items = [{"no": n, "fit": f} for n, f in [(1, 0), (2, None), (3, 1), (4, 2), (5, 1), (6, 2), (7, 0)]]
    assert [i["no"] for i in T.order(items)] == [4, 6, 3, 5, 2, 1, 7]
    assert [i["no"] for i in T.order(items, hide_unfit=True)] == [4, 6, 3, 5, 2]
    plain = [{"no": n} for n in (3, 1, 2)]
    assert [i["no"] for i in T.order(plain)] == [3, 1, 2]


def test_prompt_has_rubric_and_titles():
    t = T.Topic("x", "라벨", "설명 한 줄", "x-1")
    p = T.build_prompt(t, [(1, "싱갤", "제목   하나"), (2, "", "제목 둘")])
    assert "라벨" in p and "설명 한 줄" in p and "1. [싱갤] 제목 하나" in p and "2. 제목 둘" in p
    assert "2 = 딱 맞음" in p and "1 = 조금" in p and "0 = 아님" in p


# ---------------------------------------------------------------- CLI
def test_cli_topics_fit_dry_run_prints_and_stores_nothing(tmp_path, monkeypatch, capsys):
    items = B.sort_items(B.parse_best_list((FIX / "list_page1.html").read_text(encoding="utf-8"), NOW), "views")
    monkeypatch.setattr(T, "ROOT", tmp_path)
    write_cfg(T.CONFIG_PATH.parent, main_topic=None)
    llm = FakeFitLLM(items)
    rc = T.cli_main(["--topic", "daily_drama", "--dry-run"], fetch=lambda pages, hours: items, llm=llm)
    out = capsys.readouterr().out
    assert rc == 0 and len(llm.prompts) == 1
    lines = [ln for ln in out.splitlines() if re.match(r"^[012-]  ", ln)]
    assert [ln[0] for ln in lines] == ["2", "2", "1", "1", "0", "0"]
    assert "딱 맞음(2) 2 · 조금(1) 2 · 아님(0) 2" in out and "Codex 호출 1회" in out and "--dry-run" in out
    assert not (tmp_path / "work").exists()
    # --dry-run 이 아니면 앱이 같이 쓰도록 보관한다
    T.cli_main(["--topic", "daily_drama", "--limit", "3"], fetch=lambda pages, hours: items, llm=llm)
    assert len(list((tmp_path / "work" / "_topics").glob("fit_*_daily_drama-*.json"))) == 3


def test_cli_without_topic_and_main_dispatch(capsys):
    from recap_shorts.collect import __main__ as M
    write_cfg(T.CONFIG_PATH.parent)
    with pytest.raises(SystemExit) as e:
        M.main(["topics-fit"])                  # 대주제 없음(시험 설정) → 안내만 하고 네트워크는 안 건드림
    out = capsys.readouterr().out
    assert e.value.code == 2 and "대주제가 없습니다" in out and "daily_drama" in out         # 고를 수 있는 id 를 알려 준다
    assert T.cli_main(["--topic", "nope"]) == 2


def test_cli_failure_exit_code(tmp_path, monkeypatch, capsys):
    items = B.sort_items(B.parse_best_list((FIX / "list_page1.html").read_text(encoding="utf-8"), NOW), "views")
    monkeypatch.setattr(T, "ROOT", tmp_path)
    write_cfg(T.CONFIG_PATH.parent)

    def boom(prompt, model):
        raise RuntimeError("키 없음")
    rc = T.cli_main(["--topic", "money_life", "--dry-run"], fetch=lambda p, h: items, llm=boom)
    out = capsys.readouterr().out
    assert rc == 1 and "unavailable" in out and "키 없음" in out
