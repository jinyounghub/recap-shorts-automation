"""Persist references to existing local tools without shell rc files or secrets."""
from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_FILE = Path(__file__).resolve().parents[1] / "config" / "runtime.local.json"
FIELDS = {
    "qwen_command": "RECAP_QWEN_COMMAND_JSON",
    "qwen_python": "RECAP_QWEN_PYTHON",
    "qwen_script": "RECAP_QWEN_SCRIPT",
    "qwen_model": "RECAP_QWEN_MODEL",
    "qwen_device": "RECAP_QWEN_DEVICE",
    "qwen_timeout": "RECAP_QWEN_TIMEOUT",
    "sensevoice_dir": "RECAP_SENSEVOICE_DIR",
    "supertonic_dir": "RECAP_SUPERTONIC_DIR",
    "codex_path": "RECAP_CODEX_PATH",
    "browser_path": "RECAP_BROWSER_PATH",
    "browser_channel": "RECAP_BROWSER_CHANNEL",
}


def load_environment(path: Path | None = None) -> dict[str, str]:
    path = path or Path(os.environ.get("RECAP_RUNTIME_FILE") or DEFAULT_FILE).expanduser()
    if not path.exists():
        return {}
    config = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("runtime.local.json은 JSON 객체여야 합니다")
    unknown = set(config) - set(FIELDS)
    if unknown:
        raise ValueError("알 수 없는 로컬 실행 설정: " + ", ".join(sorted(unknown)))
    result = {}
    for field, value in config.items():
        if value is None or value == "":
            continue
        if field == "qwen_command":
            if (not isinstance(value, list) or not value or not value[0]
                    or any(not isinstance(v, str) for v in value)):
                raise ValueError("qwen_command는 기존 실행 명령의 문자열 배열이어야 합니다")
            result[FIELDS[field]] = json.dumps(value, ensure_ascii=False)
        else:
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                raise ValueError(f"로컬 실행 설정 값이 올바르지 않습니다: {field}")
            result[FIELDS[field]] = str(value)
    return result


def apply() -> None:
    # An explicitly exported value always wins. Never replace PATH, HOME,
    # CODEX_HOME, proxy/TLS settings, OAuth credentials or a model environment.
    for key, value in load_environment().items():
        os.environ.setdefault(key, value)
