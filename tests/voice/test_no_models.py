"""로컬 모델 없는 환경에서 음성 라우팅·ASR 생략·문장 재편집 계약을 검증한다."""
import json

import numpy as np
import pytest
import soundfile as sf

from recap_shorts.voice import asr, pipeline as vp, qwen, redo

SR = 24000


def _wav(path, seconds=3.0):
    t = np.arange(int(seconds * SR)) / SR
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32), SR, subtype="PCM_16")


@pytest.fixture
def local_qwen(monkeypatch):
    calls = []

    def fake_cli(text, instruct, seed, out_wav, language="Korean", work=None):
        calls.append({"text": text, "seed": seed, "instruct": instruct})
        _wav(out_wav, 3.0)
        return {"duration": 3.0, "load_sec": None, "synth_sec": 0.2, "gpu_mem_peak_mb": None,
                "gpu": None, "sample_rate": SR, "model": "existing-local-model"}

    monkeypatch.setattr(qwen, "run_cli", fake_cli)
    monkeypatch.setattr(asr, "available", lambda: False)
    return calls


def _script():
    return {"hook": "오늘의 이야기", "sentences": [{"text": "첫 문장입니다"}, {"text": "둘째 문장입니다"}],
            "closing": "끝"}


def test_qwen_without_asr_does_not_retry(tmp_path, local_qwen):
    r = qwen.synth_sentences(["첫 문장입니다", "둘째 문장입니다"], tmp_path)
    assert len(local_qwen) == 1
    assert len(r["items"]) == 2
    assert r["meta"]["model"] == "existing-local-model"
    assert r["meta"]["min_match_ok"] is True


def test_make_voice_defaults_to_qwen_and_records_original_audio(tmp_path, local_qwen):
    out = tmp_path / "voice"
    tm = vp.make_voice(_script(), out, fit=False)
    assert tm["engine"] == "qwen"
    assert tm["asr_mean_match"] is None
    assert any("RECAP_SENSEVOICE_DIR" in w for w in tm["warnings"])
    assert all((out / p).is_file() for p in tm["raw_audio"])
    assert len(tm["raw_audio"]) == len(tm["sentences"]) == 4


def test_no_asr_disables_qwen_internal_check(monkeypatch, tmp_path, local_qwen):
    def unexpected():
        raise AssertionError("--no-asr should not check or load ASR")
    monkeypatch.setattr(asr, "available", unexpected)
    tm = vp.make_voice(_script(), tmp_path / "voice", check_asr=False, fit=False)
    assert "asr_mean_match" not in tm


def test_removed_cloud_engine_fails_before_synthesis(tmp_path, local_qwen):
    with pytest.raises(ValueError, match="엔진"):
        vp.make_voice(_script(), tmp_path / "voice", engine="gemini")
    assert local_qwen == []


def test_clipping_warnings_survive_speed_fit(monkeypatch, tmp_path, local_qwen):
    monkeypatch.setattr(vp.tts, "fit_rate", lambda items, out: {
        "items": items, "factor": 1.0, "rate_before": 6.5, "rate_after": 6.5, "warnings": ["speed warning"]})
    tm = vp.make_voice(_script(), tmp_path, check_asr=False)
    assert "speed warning" in tm["warnings"]
    assert tm["clipped_ends"]
    assert any("잘렸을" in w for w in tm["warnings"])


def test_redo_qwen_uses_local_command_and_preserves_repeated_edits(monkeypatch, tmp_path, local_qwen):
    vd = tmp_path / "voice"
    tm = vp.make_voice(_script(), vd, check_asr=False, fit=False, seed=8)
    monkeypatch.setattr(redo.tts, "fit_rate", lambda items, out: {"items": items, "factor": 1.0, "warnings": []})
    out = redo.redo(vd, 1, [("고친 첫 문장", ""), ("두 번째 후보", "")])
    assert len(out["trials"]) == 1 and out["scored"] is False
    assert local_qwen[-1]["seed"] == 8
    assert out["timings"]["sentences"][1]["spoken"] == "고친 첫 문장"
    out2 = redo.redo(vd, 2, [("고친 둘째 문장", "")])
    assert out2["timings"]["sentences"][1]["spoken"] == "고친 첫 문장"
    assert out2["timings"]["sentences"][2]["spoken"] == "고친 둘째 문장"
    assert all((vd / p).is_file() for p in out2["timings"]["raw_audio"])


def test_redo_unknown_engine_never_selects_cloud(tmp_path):
    (tmp_path / "timings.json").write_text(json.dumps({"engine": "old-cloud", "sentences": []}))
    with pytest.raises(ValueError, match="로컬"):
        redo.redo(tmp_path, 0, [("새 문장", "")])
