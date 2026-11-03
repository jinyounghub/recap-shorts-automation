import json
import pytest
from recap_shorts import runtime


def test_local_configuration_preserves_exact_argv(tmp_path):
    path = tmp_path / "runtime.local.json"
    argv = ["/Users/test user/venv/bin/python", "wrapper.py", "{text_file}", "{output}", ""]
    path.write_text(json.dumps({"qwen_command": argv, "sensevoice_dir": "/models/already-installed"}))
    env = runtime.load_environment(path)
    assert json.loads(env["RECAP_QWEN_COMMAND_JSON"]) == argv
    assert env["RECAP_SENSEVOICE_DIR"] == "/models/already-installed"


def test_runtime_cannot_select_api_credentials_or_replace_codex_home(tmp_path):
    path = tmp_path / "runtime.local.json"
    for key in ("OPENAI_API_KEY", "CODEX_HOME", "api_key", "google_token"):
        path.write_text(json.dumps({key: "not-used"}))
        with pytest.raises(ValueError, match="알 수 없는"):
            runtime.load_environment(path)


def test_explicit_environment_is_preserved(tmp_path, monkeypatch):
    path = tmp_path / "runtime.local.json"
    path.write_text(json.dumps({"qwen_python": "/saved/venv/bin/python"}))
    monkeypatch.setenv("RECAP_RUNTIME_FILE", str(path))
    monkeypatch.setenv("RECAP_QWEN_PYTHON", "/existing/venv/bin/python")
    runtime.apply()
    import os
    assert os.environ["RECAP_QWEN_PYTHON"] == "/existing/venv/bin/python"
