"""Local-only defaults and migration cannot enable paid provider credentials."""
import json
import pytest
from recap_shorts import settings


def test_local_defaults():
    assert settings.image_engine() == "codex"
    assert settings.voice_engine() == "qwen"
    assert settings.model("script") == ""


def test_legacy_keys_and_remote_engines_are_not_used(monkeypatch):
    settings.path().write_text(json.dumps({"keys": {"gemini_api_key": "legacy-secret"},
                                          "image_engine": "gemini", "voice": {"engine": "gemini"},
                                          "models": {"script": "gemini-legacy", "tts": "legacy-voice"}}))
    monkeypatch.setenv("RECAP_GEMINI_API_KEY", "env-secret")
    assert settings.image_engine() == "codex"
    assert settings.voice_engine() == "qwen"
    assert settings.model("script") == ""
    view = json.dumps(settings.public_view())
    assert "legacy-secret" not in view and "env-secret" not in view
    settings.save(image_engine="off")
    assert "keys" not in json.loads(settings.path().read_text())


def test_no_api_key_configuration():
    with pytest.raises(settings.SettingsError, match="API"):
        settings.save(keys={"gemini_api_key": "key"})
    for remote in ("gemini", "local", "cloudflare"):
        with pytest.raises(settings.SettingsError):
            settings.save(image_engine=remote)
    with pytest.raises(settings.SettingsError):
        settings.save(voice={"engine": "gemini"})


def test_save_preserves_local_settings_and_explicit_model():
    settings.save(voice={"engine": "qwen", "sid": 3}, models={"script": "user-selected-model"})
    settings.save(image_engine="off", sfx={"fill_missing": False})
    assert settings.voice_engine() == "qwen"
    assert settings.supertonic_sid() == 3
    assert settings.model("script") == "user-selected-model"
    assert not settings.sfx_fill_missing()
    assert settings.image_engine() == "off"
    settings.save(models={"script": None})
    assert settings.model("script") == ""


def test_environment_model_and_paths(monkeypatch):
    monkeypatch.setenv("RECAP_SCRIPT_MODEL", "environment-model")
    assert settings.model("script") == "environment-model"
    with pytest.raises(settings.SettingsError):
        settings.save(models={"script": "changed-model"})
    monkeypatch.setenv("RECAP_TTS_ENGINE", "supertonic")
    assert settings.voice_engine() == "supertonic"


def test_corrupt_file_is_safe_default():
    settings.path().write_text("{bad")
    assert settings.voice_engine() == "qwen"
    assert settings.image_engine() == "codex"
