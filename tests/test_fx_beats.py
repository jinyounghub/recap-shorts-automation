import pytest

from recap_shorts.fx import beats as B
from recap_shorts.fx.config import BEAT_LABELS

TEXTS = ["커뮤니티 4억짜리 영수증 올라왔어", "이게 실화냐 ㄷㄷ", "근데 이게 맞아?", "알고 보니 사실이었다", "그래서 다들 화났다", "끝."]


def test_rule_fallback_labels():
    got = B.rule_beats(TEXTS)
    assert got == ["neutral", "shock", "question", "reveal", "angry", "conclusion"]
    assert all(g in BEAT_LABELS for g in got)


def test_no_llm_uses_rules(tmp_path):
    r = B.label_beats(TEXTS, cache_path=tmp_path / "fx" / "beats.json", use_llm=False)
    assert r["source"] == "rules" and r["beats"] == B.rule_beats(TEXTS)
    assert (tmp_path / "fx" / "beats.json").is_file()


def test_llm_ok_and_cache_prevents_second_call(tmp_path):
    calls = []

    def llm(prompt):
        calls.append(prompt)
        return {"beats": ["neutral", "shock", "question", "reveal", "angry", "conclusion"]}

    cp = tmp_path / "b.json"
    r = B.label_beats(TEXTS, cache_path=cp, llm=llm)
    assert r["source"] == "llm" and r["beats"][4] == "angry" and len(calls) == 1
    r2 = B.label_beats(TEXTS, cache_path=cp, llm=llm)
    assert r2["source"] == "cache" and r2["beats"] == r["beats"] and len(calls) == 1


def test_changed_sentences_invalidate_cache(tmp_path):
    cp = tmp_path / "b.json"
    calls = []

    def llm(prompt):
        calls.append(1)
        return {"beats": ["shock"] * (len(TEXTS) - 1)}

    B.label_beats(TEXTS[:-1], cache_path=cp, llm=llm)
    B.label_beats(TEXTS[:-2] + ["다른 문장"], cache_path=cp, llm=llm)
    assert len(calls) == 2


def test_llm_wrong_length_falls_back(tmp_path):
    r = B.label_beats(TEXTS, cache_path=tmp_path / "b.json", llm=lambda p: {"beats": ["shock"]})
    assert r["source"] == "rules" and r["beats"] == B.rule_beats(TEXTS) and r["error"]


def test_llm_bad_label_replaced_by_rule(tmp_path):
    r = B.label_beats(TEXTS, cache_path=tmp_path / "b.json",
                      llm=lambda p: {"beats": ["??", "SHOCK", "question", "reveal", "sad", "conclusion"]})
    assert r["source"] == "llm"
    assert r["beats"][0] == "neutral"  # 모르는 라벨 -> 규칙 값
    assert r["beats"][1] == "shock" and r["beats"][4] == "sad"


def test_llm_exception_falls_back_without_raising(tmp_path):
    def boom(prompt):
        raise RuntimeError("no key")

    r = B.label_beats(TEXTS, cache_path=tmp_path / "b.json", llm=boom)
    assert r["source"] == "rules" and "RuntimeError" in r["error"]
    calls = []  # 실패 기록도 다시 부르지 않는다
    r2 = B.label_beats(TEXTS, cache_path=tmp_path / "b.json", llm=lambda p: calls.append(1) or {"beats": []})
    assert r2["source"] == "cache" and not calls


def test_prompt_mentions_all_sentences_and_labels():
    p = B.build_prompt(TEXTS)
    assert all(t in p for t in TEXTS) and all(label in p for label in BEAT_LABELS)


@pytest.mark.parametrize("model", [None, "gemini-3.8-flash"])
def test_default_provider_is_codex_and_legacy_model_uses_current_config(monkeypatch, model):
    from recap_shorts.llm import codex

    calls = []

    def generate(prompt, **kwargs):
        calls.append(kwargs)
        return {"beats": ["shock"] * len(TEXTS)}

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("RECAP_FX_NO_LLM", raising=False)
    monkeypatch.setattr(codex, "generate_text", generate)
    result = B.label_beats(TEXTS, model=model)
    assert result["source"] == "llm" and result["beats"] == ["shock"] * len(TEXTS)
    assert len(calls) == 1 and calls[0]["model"] is None
