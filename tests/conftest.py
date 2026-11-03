import pytest


@pytest.fixture(autouse=True)
def _settings_isolated(tmp_path_factory, monkeypatch):
    """키는 시험마다 빈 설정 파일에서 읽는다 — 저장소의 config/settings.local.json 이나 개발 PC 환경변수가
    시험 결과를 바꾸지 않게. 모델 폴더 환경변수도 지운다(저장소 안 models/ 를 그대로 본다)."""
    d = tmp_path_factory.mktemp("settings")
    monkeypatch.setenv("RECAP_SETTINGS_FILE", str(d / "settings.local.json"))
    for name in ("RECAP_GEMINI_API_KEY", "RECAP_CF_ACCOUNT_ID", "RECAP_CF_API_TOKEN",
                 "RECAP_QWEN_COMMAND_JSON", "RECAP_QWEN_PYTHON", "RECAP_QWEN_SCRIPT",
                 "RECAP_QWEN_MODEL", "RECAP_QWEN_DEVICE", "RECAP_QWEN_TIMEOUT",
                 "RECAP_TTS_ENGINE", "RECAP_IMAGE_ENGINE", "RECAP_CODEX_PATH",
                 "RECAP_SENSEVOICE_DIR", "RECAP_SUPERTONIC_DIR",
                 "RECAP_SCRIPT_MODEL", "RECAP_TTS_MODEL", "RECAP_GEMINI_IMAGE_MODEL", "RECAP_TTS_SID"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _channel_topic_off(tmp_path_factory, monkeypatch):
    """채널 대주제 설정을 시험마다 빈 임시 경로로 돌린다 — 저장소의 config/channel.json(기본값 있음)·
    사용자의 config/channel.local.json 이 시험 결과를 바꾸지 않게. 기본은 '대주제 없음'(점수·LLM 호출 없음)."""
    from recap_shorts.collect import topicfit
    d = tmp_path_factory.mktemp("channel_cfg")
    monkeypatch.setattr(topicfit, "CONFIG_PATH", d / "channel.json")
    monkeypatch.setattr(topicfit, "LOCAL_PATH", d / "channel.local.json")


@pytest.fixture(autouse=True)
def _no_asr_for_word_cuts(monkeypatch):
    """문장 안 컷 시각용 받아쓰기(recap_shorts/wordcuts.py load_asr)를 끈다 — 시험이 모델·디스크 상태에 기대지 않게."""
    monkeypatch.setenv("RECAP_WORDCUTS_ASR", "off")
