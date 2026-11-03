"""화면에 가는 오류 문구 — 예외 이름·Traceback·이 PC 경로가 없고, 원인(키 없음/키 틀림/한도/연결)이 한국어로 갈린다."""
import json
import re

import pytest
import requests

from recap_shorts import errors
from recap_shorts.llm.codex import CodexError

BAD_KEY_BODY = "API key not valid. Please pass a valid API key."
PATH_LIKE = re.compile(r"[A-Za-z]:\\|Traceback \(most recent call last\)|File \"[^\"]+\", line \d+")
CLASS_NAMES = ("RuntimeError", "CodexError", "HTTPError", "ConnectionError", "OSError", "ValueError", "KeyError")


def clean(text: str) -> None:
    assert text and not PATH_LIKE.search(text), text
    assert not any(n in text for n in CLASS_NAMES), text


def resp(code: int) -> requests.Response:
    r = requests.Response()
    r.status_code = code
    return r


def test_screen_text_classifies_known_causes():
    bad = RuntimeError(f"Codex API 실패(gemini-4-argon): HTTP 400: {BAD_KEY_BODY}")
    assert errors.screen_text(bad) == "Codex ChatGPT 인증을 확인하세요 — 필요하면 codex login 으로 다시 로그인하세요"
    assert errors.screen_text(RuntimeError("Codex API 실패(x): HTTP 401: nope")).startswith("Codex ChatGPT 인증")
    assert errors.screen_text(RuntimeError("Codex API 키가 없습니다 — 화면 오른쪽 위 '설정'에서 넣으세요")) \
        == "Codex ChatGPT 로그인이 필요합니다 — codex login status 로 확인하세요"
    quota = errors.screen_text(RuntimeError("Codex API 실패(x): HTTP 429: quota"))
    assert "사용 한도" in quota
    net = errors.screen_text(RuntimeError("Codex API 연결 실패(x): ConnectionError"))
    assert net.startswith("Codex 연결 실패")
    for t in (errors.screen_text(bad), quota, net):
        clean(t)


def test_screen_text_never_leaks_class_or_path():
    e = RuntimeError(r"C:\Users\somebody\AppData\Local\recap\log.txt")
    t = errors.screen_text(e)
    clean(t)
    assert "somebody" not in t
    # 한국어 메시지는 그대로 사람에게 보여 준다(시험용 가짜 오류 — '일부러 실패' 같은 안내)
    assert errors.screen_text(RuntimeError("voice 일부러 실패")) == "voice 일부러 실패"
    # 읽을 한국어가 없으면 기본 문구
    assert errors.screen_text(RuntimeError("boom")) == errors.DEFAULT_FALLBACK
    # 슬래시로 쓴 경로도 가리고, 주소(https://)는 경로로 보지 않는다
    t = errors.screen_text(RuntimeError("파일을 찾지 못함 C:/Users/somebody/work/x.png"))
    assert "somebody" not in t and t.startswith("파일을 찾지 못함")
    assert "https://example.com/a" in errors.screen_text(RuntimeError("주소를 열지 못함 https://example.com/a"))


def test_expired_key_is_bad_key():
    e = RuntimeError("Codex API 실패(x): HTTP 400: API key expired. Please renew the API key.")
    assert errors.kind_of(e) == errors.BAD_KEY
    assert errors.kind_for_http(400, "API key expired. Please renew the API key.") == errors.BAD_KEY


def test_reason_is_short_and_classified():
    assert errors.reason(RuntimeError("Codex API 실패: HTTP 400: API key not valid")) == "키가 맞지 않음(HTTP 400)"
    assert errors.reason(RuntimeError("Codex API 실패: HTTP 429: quota")) == "사용 한도(HTTP 429)"
    assert errors.reason(RuntimeError("연결 실패: timeout")) == "연결 실패"
    assert errors.reason(RuntimeError("Codex API 키가 없습니다")) == "키 없음"
    assert errors.reason(RuntimeError("모델 답의 모양이 맞지 않음")) == "모델 답의 모양이 맞지 않음"
    assert len(errors.reason(RuntimeError("Codex API 실패: HTTP 400: " + BAD_KEY_BODY))) < 40   # 80자 잘림 없음


def test_class_name_inside_sentence_is_removed():
    """문장 가운데 남은 예외 이름(이미지 서버 연결 실패(ConnectionError) 같은)도 화면 문구에는 남기지 않는다."""
    for msg in ("이미지 서버 연결 실패(ConnectionError)", "이미지 검증 실패: OSError",
                "파일을 읽지 못함: FileNotFoundError", "그림을 열지 못함: UnidentifiedImageError"):
        t = errors.screen_text(RuntimeError(msg))
        clean(t)
        assert "(" not in t and ")" not in t, t                      # 이름을 지운 빈 괄호도 남기지 않는다
    assert errors.screen_text(RuntimeError("이미지 검증 실패: OSError")) == "이미지 검증 실패"


def test_typed_codex_error_kinds():
    e = errors.mark(CodexError("x"), errors.BAD_KEY, http=403)
    assert errors.kind_of(e) == errors.BAD_KEY and errors.http_of(e) == 403
    assert errors.kind_of(requests.HTTPError("400", response=resp(400))) == errors.HTTP
    assert errors.kind_of(requests.ConnectionError("down")) == errors.NET


def test_http_error_401_is_bad_key():
    assert errors.kind_of(requests.HTTPError("nope", response=resp(401))) == errors.BAD_KEY
    assert errors.reason(requests.HTTPError("nope", response=resp(403))) == "키가 맞지 않음(HTTP 403)"


def test_topicfit_reason_has_no_raw_error():
    from recap_shorts.collect import topicfit as T
    why = errors.reason(RuntimeError("Codex API 실패: HTTP 429: quota"))
    assert "RuntimeError" not in why and "Codex API 실패:" not in why and "429" in why


def test_error_kind_never_leaks_into_json():
    note = {"fit_note": errors.reason(RuntimeError("CodexError: HTTP 400: API key not valid"))}
    s = json.dumps(note, ensure_ascii=False)
    assert "CodexError" not in s and "API key" not in s
