"""기존 로컬 실행기 연결: 셸 해석 없이 argv 전달, 실패/거짓 성공 및 장치 선택."""
import importlib.util
import json
import subprocess
import sys
import venv
from pathlib import Path
from types import SimpleNamespace

import pytest

from recap_shorts.voice import qwen


@pytest.fixture(autouse=True)
def clear_local_config(monkeypatch):
    for name in ("RECAP_QWEN_COMMAND_JSON", "RECAP_QWEN_PYTHON", "RECAP_QWEN_SCRIPT",
                 "RECAP_QWEN_MODEL", "RECAP_QWEN_TIMEOUT", "RECAP_QWEN_DEVICE"):
        monkeypatch.delenv(name, raising=False)


def _command(monkeypatch, script: Path, *args):
    monkeypatch.setenv("RECAP_QWEN_COMMAND_JSON", json.dumps([sys.executable, str(script), *args]))


def _fake_script(tmp_path):
    path = tmp_path / "existing model env" / "synth.py"
    path.parent.mkdir()
    path.write_text('''import json, sys, wave, struct
from pathlib import Path
request = json.loads(Path(sys.argv[1]).read_text())
request["extra_argv"] = sys.argv[2:]
request["python_prefix"] = sys.prefix
Path(request["out"]).with_suffix(".request.json").write_text(json.dumps(request, ensure_ascii=False))
with wave.open(request["out"], "wb") as out:
    out.setnchannels(1)
    out.setsampwidth(2)
    out.setframerate(24000)
    out.writeframes(b"".join(struct.pack("<h", 1000 if i % 2 else -1000) for i in range(2400)))
print("Existing local runtime completed; no metadata JSON required.")
''', encoding="utf-8")
    return path


def test_existing_command_supports_paths_with_spaces_and_preserves_input(monkeypatch, tmp_path):
    script = _fake_script(tmp_path)
    _command(monkeypatch, script, "{request_json}", "")
    monkeypatch.setenv("RECAP_QWEN_MODEL", "/existing models/my-voice")
    out = tmp_path / "video output" / "voice.wav"
    text = "달러 $(touch should-not-exist) `echo nope` {output} 따옴표 ' 원문"
    info = qwen.run_cli(text, "기존 말투", 7, out)
    recorded = json.loads(out.with_suffix(".request.json").read_text())
    assert recorded["text"] == text
    assert recorded["model"] == "/existing models/my-voice"
    assert recorded["extra_argv"] == [""]
    assert info["duration"] == pytest.approx(0.1)
    assert info["sample_rate"] == 24000
    assert info["gpu"] is None
    assert not (tmp_path / "should-not-exist").exists()


def test_existing_venv_interpreter_keeps_its_environment(monkeypatch, tmp_path):
    """A venv Python symlink must not be resolved to the base interpreter."""
    existing_env = tmp_path / "existing qwen env"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(existing_env)
    monkeypatch.setenv("RECAP_QWEN_PYTHON", str(existing_env / "bin" / "python"))
    monkeypatch.setenv("RECAP_QWEN_SCRIPT", str(_fake_script(tmp_path)))
    out = tmp_path / "out.wav"
    qwen.run_cli("기존 환경 그대로", "", 1, out)
    recorded = json.loads(out.with_suffix(".request.json").read_text())
    assert Path(recorded["python_prefix"]) == existing_env


def test_text_placeholder_is_only_expanded_once(monkeypatch, tmp_path):
    script = tmp_path / "capture.py"
    script.write_text("import sys\nfrom pathlib import Path\nPath(sys.argv[2]).with_suffix('.txt').write_text(sys.argv[1])\n")
    _command(monkeypatch, script, "{text}", "{output}")
    with pytest.raises(qwen.QwenUnavailable, match="WAV"):
        qwen.run_cli("literal {output} {seed}", "", 5, tmp_path / "out.wav")
    assert (tmp_path / "out.txt").read_text() == "literal {output} {seed}"


@pytest.mark.parametrize("value", ['"python script.py"', '{}', '[]', '[42]', '["python"]', '["", "{output}"]'])
def test_invalid_command_config_rejected(monkeypatch, value):
    monkeypatch.setenv("RECAP_QWEN_COMMAND_JSON", value)
    with pytest.raises(qwen.QwenUnavailable):
        qwen.configured_command()


def test_no_output_rejects_stale_previous_wav(monkeypatch, tmp_path):
    script = tmp_path / "no_op.py"
    script.write_text("pass\n")
    _command(monkeypatch, script, "{output}")
    output = tmp_path / "out.wav"
    output.write_bytes(b"previous attempt")
    with pytest.raises(qwen.QwenUnavailable, match="WAV"):
        qwen.run_cli("test", "", 1, output)


def test_nonzero_exit_is_error(monkeypatch, tmp_path):
    script = tmp_path / "fail.py"
    script.write_text("raise SystemExit(3)\n")
    _command(monkeypatch, script, "{output}")
    with pytest.raises(qwen.QwenUnavailable, match=r"실패\(3\)"):
        qwen.run_cli("test", "", 1, tmp_path / "out.wav")


def test_timeout_is_actionable(monkeypatch, tmp_path):
    _command(monkeypatch, _fake_script(tmp_path), "{request_json}")
    def timeout(*args, **kw):
        raise subprocess.TimeoutExpired(args[0], kw["timeout"])
    monkeypatch.setattr(qwen.subprocess, "run", timeout)
    with pytest.raises(qwen.QwenUnavailable, match="제한"):
        qwen.run_cli("test", "", 1, tmp_path / "out.wav")


def _runner():
    path = Path(__file__).resolve().parents[2] / "tools" / "qwen_tts" / "synth_cli.py"
    spec = importlib.util.spec_from_file_location("qwen_synth_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _torch(cuda=False, mps=False):
    return SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: cuda),
                           backends=SimpleNamespace(mps=SimpleNamespace(is_available=lambda: mps)))


def test_mac_mps_selected_and_explicit_cpu_respected():
    select = _runner().select_device
    assert select(_torch(mps=True)) == "mps"
    assert select(_torch(mps=True), "cpu") == "cpu"
    assert select(_torch()) == "cpu"
    with pytest.raises(ValueError, match="MPS"):
        select(_torch(), "mps")


def test_runner_requires_existing_model_before_importing_heavy_packages(tmp_path, monkeypatch):
    req = tmp_path / "request.json"
    req.write_text(json.dumps({"text": "test", "out": str(tmp_path / "out.wav")}))
    monkeypatch.setenv("HF_HOME", "/existing/user/cache")
    assert _runner().main([str(req)]) == 2
    import os
    assert os.environ["HF_HOME"] == "/existing/user/cache"
