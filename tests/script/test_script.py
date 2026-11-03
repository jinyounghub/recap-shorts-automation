import json
from pathlib import Path

import pytest

from recap_shorts.script.guard import evidence_in_source, looks_factual, run_guard
from recap_shorts.script.llm import parse_json_loose
from recap_shorts.script.prompt import build_prompt
from recap_shorts.script.text import count_syllables, normalize_loose, num_to_korean, spoken_form

FIX = Path(__file__).parents[1] / "fixtures" / "script"
TOPIC = json.loads((FIX / "topic_sample.json").read_text(encoding="utf-8"))
SRC = TOPIC["source_text"]


def test_syllables():
    assert count_syllables("안녕하세요, 반갑습니다!") == 10
    assert count_syllables("980엔") == 5      # 구백팔십 + 엔
    assert count_syllables("KBO 3점") == 5
    assert num_to_korean("10000") == "만"
    assert num_to_korean("2025") == "이천이십오"
    assert spoken_form("3.5") == "삼점오"


def test_normalize():
    assert normalize_loose(' "일흔 두  살"이라는데… ') == normalize_loose("일흔두살이라는데")


def test_evidence_matching():
    assert evidence_in_source("올해 일흔 두 살이라는데", SRC)
    assert evidence_in_source("“올해 일흔두살이라는데”", SRC)       # 따옴표·공백 정규화
    assert not evidence_in_source("올해 여든 살이라는데", SRC)
    assert not evidence_in_source(None, SRC)


def test_looks_factual():
    assert looks_factual("4대 3으로 이겼다는데")
    assert looks_factual("KBO 소속이래")
    assert looks_factual("'철수'라고 함")
    assert not looks_factual("그러더니 갑자기 분위기가 달라졌다는데")


def _script(sents, hook="오늘자 동호회 레전드 경기 터졌다", closing="이게 말이 되냐 진짜"):
    return {"title_lines": ["일흔 두 살 투수가", "삼진 두 개를 잡았다"], "hook": hook, "sentences": sents,
            "closing": closing, "flags": []}


def S(text, ev=None, hint="일러스트"):
    return {"text": text, "evidence": ev, "source_id": None, "emphasis": None, "image_hint": hint}


def test_guard_deletes_unsupported_facts():
    sents = [
        S("상대 투수가 쥐가 났다는데", "상대팀 투수가 갑자기 쥐가 나서 마운드에서 주저앉음"),
        S("그 투수가 시속 150킬로를 던졌다고 함", "시속 150킬로를 던졌음"),          # 근거 원문에 없음 -> 삭제
        S("우리가 4대 3으로 이겼다는데"),                                          # 숫자 있는데 근거 없음 -> 삭제
        S("그러더니 분위기가 이상해졌다는데"),                                      # 연결 문장 -> 유지
        S("구독 눌러 주세요", None),                                               # 금지 표현 -> 삭제
    ]
    out, chk = run_guard(_script(sents), TOPIC)
    assert [s["text"] for s in out["sentences"]] == ["상대 투수가 쥐가 났다는데", "그러더니 분위기가 이상해졌다는데"]
    assert [d["index"] for d in chk["deleted"]] == [1, 2, 4]
    assert not chk["ok"]
    assert any("삭제" in f for f in out["flags"])


def test_guard_format_checks():
    out, chk = run_guard(_script([S("그러더니 분위기가 이상해졌다는데")], hook="짧은 훅"), TOPIC)
    assert not chk["checks"]["hook"]["ok"]
    assert not chk["checks"]["syllables"]["ok"]
    bad_title = _script([S("그러더니")])
    bad_title["title_lines"] = ["열네 글자를 훨씬 넘기는 아주 긴 제목 줄입니다", "둘째"]
    _, chk2 = run_guard(bad_title, TOPIC)
    assert not chk2["checks"]["title_lines"]["ok"]


def test_guard_hook_noun_phrase():
    # J 꼴(명사로 끝남, 12~20음절)은 통과, 서술어로 끝나면 경고
    _, ok = run_guard(_script([S("그러더니")], hook="일흔 두 살에 삼진을 잡았다는 투수 근황"), TOPIC)
    assert ok["checks"]["hook"]["ok"] and ok["checks"]["hook"]["ends_noun"]
    out, bad = run_guard(_script([S("그러더니")], hook="실베에 일흔 두 살 투수 얘기가 올라왔어."), TOPIC)
    assert not bad["checks"]["hook"]["ends_noun"] and not bad["checks"]["hook"]["ok"]
    assert any("문장으로 끝남" in f for f in out["flags"])


def test_guard_duplicate_hook_and_hook_number():
    hook = "오늘자 동호회 레전드 경기 터졌다"
    out, chk = run_guard(_script([S(hook), S("그러더니 분위기가 이상해졌다는데")], hook=hook, closing="진짜 99점짜리 경기"), TOPIC)
    assert len(out["sentences"]) == 1
    assert any("99" in f for f in out["flags"])


def test_prompt_contains_rules():
    p = build_prompt(TOPIC)
    for must in ("14자", "12~20음절", "서술어로 끝내지 않는다", "190~220", "반말", "~했다는데", "구독", "evidence", "글자 그대로", "원문에 없는 숫자",
                 "image_hint", "실제 사진 필요(사용자/라이선스)", "전해 들은 말", "국야갤", "null"):
        assert must in p, must
    assert TOPIC["source_text"].strip() in p


def test_parse_json_loose():
    assert parse_json_loose('앞말 {"a": {"b": 2}} 뒷말') == {"a": {"b": 2}}
    with pytest.raises(ValueError):
        parse_json_loose("없음")


def test_parse_json_loose_forgives_raw_controls_and_bad_escapes():
    """긴 글을 옮겨 적은 답에서 가끔 나오는 것: 문자열 속 날 탭·줄바꿈, JSON 에 없는 이스케이프."""
    assert parse_json_loose('{"text": "가\t나\n다"}') == {"text": "가\t나\n다"}
    assert parse_json_loose(r'{"text": "it\'s \[제보\] C:\new"}') == {"text": "it\\'s \\[제보\\] C:\new"}
    # 제대로 된 이스케이프(\\, \", \n, \u)는 그대로 읽는다 — 고쳐 읽기에서 \\[ 가 망가지지 않게
    assert parse_json_loose(r'{"a": "x\\[y \"z\" \u00e9 \q"}') == {"a": 'x\\[y "z" é \\q'}
    with pytest.raises(ValueError):
        parse_json_loose('{"text": "그가 "차" 했다"}')      # 안 감싼 따옴표는 고치지 않는다(그림 읽기는 다시 묻는다)


def test_pipeline_with_fake_llm(tmp_path):
    from recap_shorts.script.pipeline import generate_script

    class L:
        calls = [{"seconds": 0.1, "ok": True}]

        def complete_json(self, prompt):
            return _script([S("상대 투수가 쥐가 났다는데", "상대팀 투수가 갑자기 쥐가 나서 마운드에서 주저앉음"),
                            S("지어낸 숫자 77이 나왔다는데")])
    sc, chk = generate_script(TOPIC, tmp_path, L())
    assert len(sc["sentences"]) == 1 and len(chk["deleted"]) == 1
    assert (tmp_path / "script.json").exists() and (tmp_path / "script_check.json").exists()


def test_guard_title_number_flag():
    sc = _script([S("그러더니 분위기가 이상해졌다는데")])
    sc["title_lines"] = ["72세 할배가", "등판했더니"]
    out, _ = run_guard(sc, TOPIC)
    assert any(f.startswith("제목: 숫자") for f in out["flags"])
