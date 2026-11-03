"""Offline subprocess contract tests: no Codex account or network is required."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest
from PIL import Image

from recap_shorts import errors
from recap_shorts.llm.codex import CodexCallLimit, CodexError, CodexLLM, CodexUnavailable


@pytest.fixture
def fake_codex(tmp_path, monkeypatch):
    binary = tmp_path / "codex fake"
    capture = tmp_path / "call.json"
    binary.write_text(f"#!{sys.executable}\n" + r'''
import json, os, pathlib, sys, time
args = sys.argv[1:]
prompt = sys.stdin.read()
images = [args[i + 1] for i, value in enumerate(args) if value == "--image"]
record = {
    "args": args, "prompt": prompt, "cwd": os.getcwd(),
    "has_api_key": "OPENAI_API_KEY" in os.environ or "CODEX_API_KEY" in os.environ,
    "has_alt_auth": "CODEX_ACCESS_TOKEN" in os.environ or "OPENAI_BASE_URL" in os.environ,
    "home_present": bool(os.environ.get("HOME")),
    "images": [pathlib.Path(p).read_bytes()[:8].hex() for p in images],
}
pathlib.Path(os.environ["FAKE_CODEX_CAPTURE"]).write_text(json.dumps(record))
mode = os.environ.get("FAKE_CODEX_MODE", "ok")
if mode == "timeout":
    time.sleep(10)
if mode == "nonzero":
    print("private diagnostic sk-test-must-not-be-logged", file=sys.stderr)
    sys.exit(7)
if mode == "classified_error":
    print(os.environ["FAKE_CODEX_ERROR"] + " sk-test-private", file=sys.stderr)
    sys.exit(1)
if mode == "missing":
    sys.exit(0)
out = pathlib.Path(args[args.index("--output-last-message") + 1])
if mode == "malformed":
    out.write_text("not json")
else:
    value = {"result": json.dumps({"hook": "안녕", "sentences": []}, ensure_ascii=False)}
    if mode == "bad_envelope":
        value = {"result": {"a": 1}}
    elif mode == "empty":
        value = {"result": "  "}
    elif mode == "bad_inner":
        value = {"result": "No object was produced"}
    out.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
''', encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("FAKE_CODEX_CAPTURE", str(capture))
    return str(binary), capture


def test_saved_oauth_cli_receives_prompt_stdin_and_never_inherits_paid_keys(fake_codex, monkeypatch):
    exe, capture = fake_codex
    for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "OPENAI_BASE_URL"):
        monkeypatch.setenv(key, "must-stay-in-parent-only")
    prompt = "한글" * 30000 + " $(do-not-execute) `literal`"
    adapter = CodexLLM(exe=exe)
    assert adapter.complete_json(prompt) == {"hook": "안녕", "sentences": []}
    record = json.loads(capture.read_text())
    assert prompt in record["prompt"] and prompt not in record["args"]
    assert record["args"][-1] == "-"
    assert not record["has_api_key"] and not record["has_alt_auth"]
    assert record["home_present"]
    assert 'forced_login_method="chatgpt"' in record["args"]
    assert 'model_provider="openai"' in record["args"]
    for setting in ("features.shell_tool=false", "features.apps=false", "features.multi_agent=false", 'web_search="disabled"'):
        assert setting in record["args"]
    assert record["args"][record["args"].index("--sandbox") + 1] == "read-only"
    assert "--model" not in record["args"]
    assert "--skip-git-repo-check" in record["args"]
    assert not Path(record["cwd"]).exists()  # temporary source/results removed
    assert adapter.calls[0]["ok"] is True
    assert "OPENAI_API_KEY" in __import__("os").environ


def test_explicit_model_and_image_inputs_are_passed_to_codex(fake_codex, tmp_path):
    exe, capture = fake_codex
    image = Image.new("RGB", (20, 30), "red")
    file = tmp_path / "a.png"
    image.save(file)
    data = io.BytesIO()
    image.save(data, "PNG")
    adapter = CodexLLM("my-current-codex-model", exe=exe)
    answer = adapter._complete("Read these three images", images=[file, data.getvalue(), image], json=True)
    assert answer["hook"] == "안녕"
    record = json.loads(capture.read_text())
    assert record["args"][record["args"].index("--model") + 1] == "my-current-codex-model"
    assert record["images"] == ["89504e470d0a1a0a"] * 3
    assert adapter.calls[0]["images"] == 3


@pytest.mark.parametrize("mode", ["missing", "malformed", "bad_envelope", "empty", "bad_inner", "nonzero"])
def test_success_requires_real_valid_output_file(fake_codex, monkeypatch, mode):
    exe, capture = fake_codex
    monkeypatch.setenv("FAKE_CODEX_MODE", mode)
    adapter = CodexLLM(exe=exe)
    with pytest.raises(CodexError) as error:
        adapter.complete_json("p")
    assert "sk-test" not in str(error.value)
    assert adapter.calls[0]["ok"] is False
    assert not Path(json.loads(capture.read_text())["cwd"]).exists()


def test_timeout_counts_once_and_call_limit_stops_before_spawn(fake_codex, monkeypatch):
    exe, _ = fake_codex
    monkeypatch.setenv("FAKE_CODEX_MODE", "timeout")
    adapter = CodexLLM(exe=exe, timeout=0.2, max_calls=1)
    with pytest.raises(CodexError, match="시간"):
        adapter.complete_json("p")
    with pytest.raises(CodexCallLimit):
        adapter.complete_json("p")
    assert len(adapter.calls) == 1 and adapter.calls[0]["error"] == "timeout"


@pytest.mark.parametrize("diagnostic,kind,http", [
    ("Not logged in", errors.NO_KEY, None),
    ("Login required before inference", errors.NO_KEY, None),
    ("401 Unauthorized", errors.BAD_KEY, 401),
    ("Authentication failed", errors.BAD_KEY, None),
    ("HTTP 429 quota exceeded", errors.QUOTA, 429),
    ("Usage limit reached", errors.QUOTA, None),
])
def test_cli_auth_and_quota_failures_keep_typed_actionable_errors(fake_codex, monkeypatch, diagnostic, kind, http):
    exe, _ = fake_codex
    monkeypatch.setenv("FAKE_CODEX_MODE", "classified_error")
    monkeypatch.setenv("FAKE_CODEX_ERROR", diagnostic)
    with pytest.raises(CodexError) as caught:
        CodexLLM(exe=exe).complete_json("p")
    assert errors.kind_of(caught.value) == kind
    assert errors.http_of(caught.value) == http
    assert "sk-test" not in str(caught.value)
    if kind in (errors.NO_KEY, errors.BAD_KEY):
        assert "codex login" in str(caught.value)


def test_no_legacy_model_provider_or_unbounded_timeout(fake_codex):
    exe, _ = fake_codex
    with pytest.raises(ValueError, match="Codex 모델"):
        CodexLLM("gemini-old", exe=exe)
    for timeout in (0, float("inf"), 3601):
        with pytest.raises(ValueError, match="timeout"):
            CodexLLM(exe=exe, timeout=timeout)
    with pytest.raises(CodexUnavailable) as caught:
        CodexLLM(exe="/does/not/exist/recap-codex")
    assert errors.kind_of(caught.value) == errors.OTHER  # missing binary is not a login failure


def test_sfx_does_not_send_audio_to_vision_or_remote_provider(monkeypatch):
    from recap_shorts.fx import analyze_sfx

    monkeypatch.setattr(analyze_sfx, "clip_b64", lambda *args: pytest.fail("audio must not be sent"))
    with pytest.raises(RuntimeError, match="외부 음성 분석 API"):
        analyze_sfx.listen_batch([("sound", Path("sound.wav"), 0.0)], ["pop"], model=None, listen_s=1.0)


def test_meme_analysis_uses_codex_image_inputs(monkeypatch, tmp_path):
    from recap_shorts.fx import analyze_meme
    from recap_shorts.llm import codex

    file = tmp_path / "reaction.png"
    Image.new("RGB", (20, 30), "green").save(file)
    calls = []

    def read(images, prompt, model, **kwargs):
        calls.append((images, prompt, model))
        return '[{"k":1,"meaning":"놀람","beats":["shock"],"quality":4}]'

    monkeypatch.setattr(codex, "read_images", read)
    answer = analyze_meme.look_batch([("reaction.png", file)], ["shock"])
    assert answer["reaction.png"]["meaning"] == "놀람"
    assert calls[0][0] == [file] and calls[0][2] is None
