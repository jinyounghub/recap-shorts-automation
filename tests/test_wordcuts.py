"""문장 안 그림 컷을 어절 사이에 놓는 규칙(recap_shorts/wordcuts.py) — 소리 없이 합성한 목소리(어절 사이 0.1초 무음)로.

합성 규칙: 어절 하나 = 음절 수 x 0.16초 사인파, 어절 사이 0.1초 무음, 문장 앞뒤 0.05초 무음, 문장 사이 0.1초 무음.
음절 수에 비례해 길어서 '음절 비율 추정'이 어절 경계를 정확히 짚는다(받아쓰기 없이 시험할 수 있게).
"""
import json

import numpy as np
import pytest
import soundfile as sf

from recap_shorts import timeline, wordcuts as WC
from recap_shorts.fx import apply as A
from recap_shorts.render.captions import make_cards

SR = 24000
SYL_S, GAP_S, EDGE_S, SENT_GAP_S, LEAD_S = 0.16, 0.10, 0.05, 0.10, 0.10
DELAY = 0.2

# 어절 = (글, 음절 수)
HOOK = [("훅", 1), ("문장이", 3), ("다", 1)]
S1 = [("가나", 2), ("다라마바", 4), ("사아자", 3), ("차카", 2), ("타파하가", 4), ("나다라", 3)]          # 18음절
S2 = [("하나", 2), ("둘셋넷", 3), ("다섯여섯", 4), ("일곱", 2), ("여덟아홉", 4), ("열", 1)]


def synth(tmp_path, specs):
    """specs: [(role, [(어절, 음절)])] -> (wav 경로, timings). 어절 시각 표 words_at 도 timings 에 같이 넣는다(시험 확인용)."""
    parts, sents, pos = [np.zeros(int(LEAD_S * SR), dtype="float32")], [], LEAD_S
    for i, (role, words) in enumerate(specs):
        start = pos
        chunk = [np.zeros(int(EDGE_S * SR), dtype="float32")]
        t = start + EDGE_S
        spans = []
        for j, (w, n) in enumerate(words):
            dur = n * SYL_S
            x = (0.3 * np.sin(2 * np.pi * 220 * np.arange(int(dur * SR)) / SR)).astype("float32")
            f = int(0.005 * SR)
            x[:f] *= np.linspace(0, 1, f)
            x[-f:] *= np.linspace(1, 0, f)
            chunk.append(x)
            spans.append((round(t, 3), round(t + dur, 3)))
            t += dur
            if j < len(words) - 1:
                chunk.append(np.zeros(int(GAP_S * SR), dtype="float32"))
                t += GAP_S
        chunk.append(np.zeros(int(EDGE_S * SR), dtype="float32"))
        data = np.concatenate(chunk)
        parts.append(data)
        pos = start + len(data) / SR
        sents.append({"index": i, "role": role, "text": " ".join(w for w, _ in words), "start": round(start, 3),
                      "end": round(pos, 3), "spans": spans})
        if i < len(specs) - 1:
            parts.append(np.zeros(int(SENT_GAP_S * SR), dtype="float32"))
            pos += SENT_GAP_S
    parts.append(np.zeros(int(0.25 * SR), dtype="float32"))
    audio = np.concatenate(parts)
    wav = tmp_path / "voice.wav"
    sf.write(str(wav), audio, SR, subtype="PCM_16")
    return wav, {"sentences": sents, "duration": round(len(audio) / SR, 3)}


def in_speech(timings, idx, t):
    """wav 시각 t 가 idx 문장의 어느 어절 소리 안인가(경계 0.01초 안쪽까지)."""
    return any(a + 0.01 < t < b - 0.01 for a, b in timings["sentences"][idx]["spans"])


@pytest.fixture
def voice(tmp_path):
    wav, timings = synth(tmp_path, [("hook", HOOK), ("body", S1), ("body", S2)])
    return wav, timings


def build(timings, plan, wc=None):
    script = {"title_lines": ["a", "b"], "hook": "훅", "sentences": [{"emphasis": ""}, {"emphasis": ""}]}
    return timeline.build(script, timings, plan, voice="voice/voice.wav", word_cuts=wc)


def plan(n1=2, n2=3):
    return {"0": ["h.png"], "1": [f"a{k}.png" for k in range(n1)], "2": [f"b{k}.png" for k in range(n2)]}


def cuts_of(tl, timings, idx):
    s = timings["sentences"][idx]
    end = timings["sentences"][idx + 1]["start"] if idx + 1 < len(timings["sentences"]) else 1e9
    return [x["start"] for x in tl["shots"] if s["start"] + 0.05 < x["start"] < end - 0.05]


def make_wc(voice, delay=DELAY, cfg=None):
    wav, timings = voice
    return WC.WordCuts(timings["sentences"], WC.load_energy(wav), None, delay, cfg)


def test_cuts_land_in_word_gaps_and_old_split_does_not(voice):
    wav, timings = voice
    old = build(timings, plan())
    new = build(timings, plan(), make_wc(voice))
    for idx in (1, 2):
        o, n = cuts_of(old, timings, idx), cuts_of(new, timings, idx)
        assert len(n) == len(o) and len(o) == (1 if idx == 1 else 2)
        assert all(not in_speech(timings, idx, t - DELAY) for t in n)      # 영상에서는 목소리가 DELAY 늦게 나온다
    assert any(in_speech(timings, 1, t - DELAY) for t in cuts_of(old, timings, 1))   # 예전 균등 분할은 어절 한가운데


def test_sentence_boundaries_and_picture_count_unchanged(voice):
    wav, timings = voice
    old = build(timings, plan())
    new = build(timings, plan(), make_wc(voice))
    assert [s["file"] for s in new["shots"]] == [s["file"] for s in old["shots"]]
    starts = {s["start"] for s in timings["sentences"]} | {0.0}
    keep_old = [(s["start"], s["end"]) for s in old["shots"] if s["start"] in starts]
    keep_new = [(s["start"], s["end"]) for s in new["shots"] if s["start"] in starts]
    assert [a for a, _ in keep_old] == [a for a, _ in keep_new]
    assert new["shots"][-1]["end"] == old["shots"][-1]["end"] and new["shots"][0]["start"] == 0.0
    # 샷이 이어지고(빈틈·겹침 없음) 모두 0.7초 이상
    for a, b in zip(new["shots"], new["shots"][1:]):
        assert a["end"] == b["start"]
    assert min(s["end"] - s["start"] for s in new["shots"][1:]) >= 0.7 - 1e-6


def test_off_is_the_old_equal_split(voice):
    wav, timings = voice
    off = build(timings, plan(), None)
    for i, s in enumerate(timings["sentences"]):
        files = plan()[str(s["index"])]
        a = 0.0 if i == 0 else s["start"]
        b = timings["sentences"][i + 1]["start"] if i + 1 < len(timings["sentences"]) else round(timings["duration"] + 0.45, 3)
        if i == 0:
            b = timings["sentences"][1]["start"]
        step = (b - a) / len(files)
        got = [(x["start"], x["end"]) for x in off["shots"] if x["file"] in files]
        assert got == [(round(a + j * step, 3), round(a + (j + 1) * step, 3)) for j in range(len(files))]
    assert all("breaks" not in json.dumps(c) for c in off["captions"])
    # 설정이 꺼져 있으면 for_job 은 None
    assert WC.for_job(wav.parent, timings, {"timing": {"cuts_at_word_gaps": False}}) is None
    assert WC.for_job(wav.parent, timings, {"timing": {}}) is not None            # 기본 켜짐
    assert WC.from_options(wav.parent, timings, {"fx": {"timing": {"cuts_at_word_gaps": False}}}) is None


@pytest.mark.parametrize("stage", ["timeline", "fx"])
def test_no_asr_option_prevents_model_calls_for_scene_and_fx_cuts(voice, tmp_path, monkeypatch, stage):
    """Installed ASR must remain unused after a job is created with --no-asr."""
    from recap_shorts.voice import asr
    wav, timings = voice
    job = tmp_path / "job"
    (job / "voice").mkdir(parents=True)
    (job / "voice" / "voice.wav").write_bytes(wav.read_bytes())
    calls = []
    monkeypatch.delenv("RECAP_WORDCUTS_ASR", raising=False)
    monkeypatch.setattr(asr, "available", lambda: True)
    monkeypatch.setattr(asr, "transcribe_timed", lambda x, sr: calls.append(len(x)) or
                        {"text": "가", "tokens": ["가"], "ts": [0.1]})
    options = {"check_asr": False, "fx": {"enabled": True, "beats": {"use_llm": False},
               "memes": {"enabled": True, "library": str(tmp_path / "no-meme-library")},
               "sfx": {"enabled": False}, "intro": {"enabled": False},
               "timing": {"word_cuts": {"asr": "auto"}}}}

    def run():
        if stage == "timeline":
            assert WC.from_options(job, timings, options) is not None
        else:
            result = A.maybe_apply(job, build(timings, plan()), timings, options)
            assert "beats" in result["fx"] and "error" not in result["fx"]

    run()
    assert calls == []
    assert not (job / WC.SIDECAR).exists()
    options["check_asr"] = True
    run()
    assert len(calls) == len(timings["sentences"])
    assert (job / WC.SIDECAR).is_file()


def test_too_short_sentence_falls_back_to_equal_split(tmp_path):
    wav, timings = synth(tmp_path, [("hook", HOOK), ("body", [("가나다", 3), ("라마바", 3)]), ("body", S2)])
    wc = WC.WordCuts(timings["sentences"], WC.load_energy(wav), None, DELAY, None)
    assert wc.choose(1, timings["sentences"][1]["start"], timings["sentences"][2]["start"], 3) is None     # 경계 1곳 < 컷 2곳
    old = build(timings, plan(3, 3))
    new = build(timings, plan(3, 3), wc)
    assert [(s["start"], s["end"]) for s in new["shots"] if s["file"].startswith("a")] == \
           [(s["start"], s["end"]) for s in old["shots"] if s["file"].startswith("a")]
    assert wc.errors == []


def test_avoids_glued_words_and_prefers_comma(tmp_path):
    # '못 / 구하면' 사이(붙여 읽는 말)를 피하고 쉼표 뒤를 고른다
    words = [("가나", 2), ("다라마", 3), ("못", 1), ("구하면", 3), ("사아자,", 3), ("차카타", 3), ("파하", 2), ("가나다", 3)]
    wav, timings = synth(tmp_path, [("hook", HOOK), ("body", words)])
    wc = WC.WordCuts(timings["sentences"], WC.load_energy(wav), None, 0.0, None)
    s = timings["sentences"][1]
    gaps = {g.k: g for g in wc.gaps(1)}
    assert gaps[3].glued and not gaps[2].glued and gaps[5].comma
    end = s["end"] + SENT_GAP_S
    pick = wc.choose(1, s["start"], end, 2)
    assert pick and len(pick) == 1 and pick[0].k != 3


def test_text_k_maps_spoken_to_caption_words(voice):
    wc = make_wc(voice)
    wc.sents[1] = {**wc.sents[1], "spoken": "이백사십육만 회 읽혔다"}        # 말한 꼴 3어절
    assert wc.text_k(1, 1, ["246만", "회", "읽혔다"]) == 1                    # 어절 수가 같으면 그대로
    assert wc.text_k(1, 1, ["246만회", "읽혔다"]) == 1                        # 다르면 음절 비율에서 가장 가까운 경계
    assert wc.text_k(1, 2, ["246만회", "읽혔다"]) == 1


def test_caption_cards_break_at_the_chosen_cuts(voice):
    wav, timings = voice
    wc = make_wc(voice)
    new = build(timings, plan(), wc)
    old = build(timings, plan())
    for idx in (1, 2):
        s, e = timings["sentences"][idx], (timings["sentences"][idx + 1]["start"] if idx == 1 else 1e9)
        starts = {c["start"] for c in new["captions"] if s["start"] <= c["start"] < e}
        for t in cuts_of(new, timings, idx):
            assert any(abs(t - x) < 1e-3 for x in starts)                    # 컷마다 같은 시각에 자막 카드가 바뀜
        assert min(c["end"] - c["start"] for c in new["captions"] if s["start"] <= c["start"] < e) >= 0.55
    # 글자는 그대로 이어진다
    assert " ".join(c["text"] for c in new["captions"]) == " ".join(c["text"] for c in old["captions"])


def test_make_cards_breaks_validation():
    base = {"text": "하나 둘 셋 넷 다섯 여섯 일곱 여덟", "start": 0.0, "end": 4.0, "emphasis": ""}
    plain = make_cards([base], measure=False)
    assert make_cards([{**base, "breaks": []}], measure=False) == plain
    bad = make_cards([{**base, "breaks": [[0, 1.0], [9, 2.0], [3, 9.0], ["x", 1]]}], measure=False)
    assert bad == plain                                                      # 범위 밖·시각 밖·잘못된 값은 무시
    cut = make_cards([{**base, "breaks": [[4, 2.3]]}], measure=False)
    assert any(abs(c["end"] - 2.3) < 1e-3 for c in cut) and any(abs(c["start"] - 2.3) < 1e-3 for c in cut)
    assert not any(c["text"].split()[0] in ("하나", "둘", "셋", "넷") and "다섯" in c["text"] for c in cut)  # 컷을 넘는 카드 없음


def test_fx_respread_before_meme_uses_the_same_rule(voice):
    wav, timings = voice
    wc = make_wc(voice)
    shots = build(timings, plan(3, 2))["shots"]                              # 균등 분할 상태로 시작(문장 1: 3장)
    s1 = timings["sentences"][1]
    nxt = timings["sentences"][2]["start"]
    meme = {"kind": "meme", "file": "m.png", "start": nxt - 1.2, "end": nxt}
    sents = timings["sentences"]
    old = A.fit_before_meme(shots, sents, meme, min_len=0.7)
    new = A.fit_before_meme(shots, sents, meme, min_len=0.7, word_cuts=wc)
    inner_old = [s["start"] for s in old if s["start"] > s1["start"] + 0.05 and s.get("kind") != "meme" and s["start"] < nxt - 0.05]
    inner_new = [s["start"] for s in new if s["start"] > s1["start"] + 0.05 and s.get("kind") != "meme" and s["start"] < nxt - 0.05]
    assert len(inner_new) == len(inner_old) == 2
    assert all(not in_speech(timings, 1, t - DELAY) for t in inner_new)
    assert any(in_speech(timings, 1, t - DELAY) for t in inner_old)
    assert [s["file"] for s in new] == [s["file"] for s in old]
    # 끄면(word_cuts=None) 예전 결과와 똑같다
    assert A.fit_before_meme(shots, sents, meme, min_len=0.7, word_cuts=None) == old


def test_meme_start_snaps_to_a_word_gap(voice):
    wav, timings = voice
    wc = make_wc(voice)
    nxt = timings["sentences"][2]["start"]
    ev = {"sentence": 1, "start": round(nxt - 1.2, 3), "end": nxt}
    orig = A.snap_meme_start(wc, dict(ev), timings["sentences"], {"full_shot_min_s": 0.6})
    moved = dict(ev)
    old = A.snap_meme_start(wc, moved, timings["sentences"], {"full_shot_min_s": 0.6})
    assert old == ev["start"] and orig == old
    assert abs(moved["start"] - ev["start"]) <= 0.4 + 1e-9 and moved["end"] == ev["end"]
    assert not in_speech(timings, 1, moved["start"] - DELAY)
    off = make_wc(voice, cfg={"snap_meme_s": 0})
    assert A.snap_meme_start(off, dict(ev), timings["sentences"], {}) is None


def test_asr_anchors_use_token_times_and_fill_the_rest():
    words = ["결혼", "날짜", "잡은"]
    tokens = ["결", "혼", " 날짜", " 잡", "은"]
    ts = [0.24, 0.42, 0.54, 0.84, 0.96]
    a = WC.asr_anchors(words, tokens, ts, 10.0)
    assert a[1] == pytest.approx(10.48) and a[2] == pytest.approx(10.0 + (0.69 + 0.84) / 2)
    # 못 맞춘 경계는 빠지고, 비율로 채운다
    part = WC.asr_anchors(["결혼", "날짜", "잡은"], ["결", "혼", " 날", " 엉뚱"], [0.24, 0.42, 0.54, 0.84], 0.0)
    assert 1 in part and 2 not in part
    times, used = WC.estimate_times(words, 0.0, 6.0, {1: 2.0, 2: 0.5})              # 2번은 1번보다 앞(거꾸로) → 버림
    assert used == {1} and times[1] == 2.0 and 2.0 < times[2] < 6.0


def test_asr_sidecar_is_cached_by_sentence_signature(voice, tmp_path, monkeypatch):
    wav, timings = voice
    job = tmp_path / "job"
    (job / "voice").mkdir(parents=True)
    (job / "voice" / "voice.wav").write_bytes(wav.read_bytes())
    from recap_shorts.voice import asr
    calls = []
    monkeypatch.setattr(asr, "available", lambda: True)
    monkeypatch.setattr(asr, "transcribe_timed", lambda x, sr: calls.append(len(x)) or {"text": "t", "tokens": ["가"], "ts": [0.1]})
    monkeypatch.delenv("RECAP_WORDCUTS_ASR")
    sents = timings["sentences"]
    a = WC.load_asr(job, sents, job / "voice" / "voice.wav", "auto")
    assert len(calls) == 3 and set(a) == {0, 1, 2} and (job / WC.SIDECAR).is_file()
    WC.load_asr(job, sents, job / "voice" / "voice.wav", "auto")
    assert len(calls) == 3                                                    # 저장된 것을 씀
    changed = [dict(s) for s in sents]
    changed[1]["spoken"] = "다른 말을 하게 됨"
    WC.load_asr(job, changed, job / "voice" / "voice.wav", "auto")
    assert len(calls) == 4                                                    # 바뀐 문장만 다시
    assert WC.load_asr(job, sents, job / "voice" / "voice.wav", "off") == {}
    monkeypatch.setenv("RECAP_WORDCUTS_ASR", "off")
    assert WC.load_asr(job, sents, job / "voice" / "voice.wav", "auto") == {}
    monkeypatch.delenv("RECAP_WORDCUTS_ASR")
    assert set(WC.load_asr(job, sents, job / "voice" / "voice.wav", "cache")) == {0, 2} and len(calls) == 4   # 1번은 서명이 달라져 못 씀, 모델도 안 돌림


def test_picture_lead_follows_fx_switches():
    cfg = {"enabled": True, "timing": {"picture_lead_s": 0.2}}
    assert A.picture_lead(cfg, None) == 0.2
    assert A.picture_lead({**cfg, "enabled": False}, None) == 0.0
    assert A.picture_lead(cfg, {"fx": False}) == 0.0
    both_off = {"fx": {"memes": {"enabled": False}, "sfx": {"enabled": False}}}
    assert A.picture_lead(cfg, both_off) == 0.0
    assert A.picture_lead(cfg, {"fx": {"memes": {"enabled": False}}}) == 0.2


def test_load_energy_and_speech_window(voice):
    wav, timings = voice
    en = WC.load_energy(wav)
    s = timings["sentences"][1]
    on, off, ref = WC.speech_window(en, s["start"], s["end"])
    assert on == pytest.approx(s["start"] + EDGE_S, abs=0.03) and off == pytest.approx(s["end"] - EDGE_S, abs=0.03)
    assert ref > 0.05
    assert WC.load_energy(wav.parent / "없음.wav") is None
