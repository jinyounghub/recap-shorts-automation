"""Classify local/Codex failures into concise Korean CLI and job-state messages.

Raw subprocess output is not included. Full local stack traces stay in job logs.
Typed Codex authentication and quota failures stop repeated downstream calls.
"""
from __future__ import annotations

import re

NO_KEY = "no_key"
BAD_KEY = "bad_key"
QUOTA = "quota"
NET = "net"
HTTP = "http"
OTHER = "other"
KINDS = (NO_KEY, BAD_KEY, QUOTA, NET, HTTP, OTHER)

_BAD_KEY_TEXT = re.compile(r"api[\s_-]*key[\s_-]*not[\s_-]*valid|api[\s_-]*key[\s_-]*expired|api_key_invalid|"
                           r"invalid api key|키가 맞지 않", re.I)
_NO_KEY_TEXT = re.compile(r"api 키가 없습니다|api key is missing|no api key|키가 없", re.I)
_QUOTA_TEXT = re.compile(r"resource_exhausted|quota|사용 한도|한도를 다 썼", re.I)
_NET_TEXT = re.compile(r"connectionerror|connection aborted|connection reset|timed out|timeout|"
                       r"name or service|연결 실패|연결하지 못", re.I)
_HTTP_TEXT = re.compile(r"http\s*(\d{3})", re.I)
_PATH_TEXT = re.compile(r"(?:(?<![A-Za-z])[A-Za-z]:[\\/]|\\\\)[^\s'\"<>|]*")   # C:\… C:/… \\서버\…
_EXC_PREFIX = re.compile(r"^\s*[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception)\s*:\s*")
_CLASS_TOKEN = re.compile(r"\b[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception)\b")   # 문장 가운데 남은 예외 이름
_TRACEBACK = re.compile(r"Traceback \(most recent call last\):|File \"[^\"]*\", line \d+", re.I)
_HANGUL = re.compile(r"[가-힣]")

DEFAULT_FALLBACK = "알 수 없는 문제로 멈췄습니다"


def _text_of(e) -> str:
    return e if isinstance(e, str) else str(e)


def _type_name(e) -> str:
    return "" if isinstance(e, str) else type(e).__name__


def http_of(e) -> int | None:
    """예외에 붙은 HTTP 코드(있으면). requests.HTTPError 의 response 에서도 읽는다."""
    v = getattr(e, "http", None)
    if isinstance(v, int):
        return v
    resp = getattr(e, "response", None)
    code = getattr(resp, "status_code", None)
    if isinstance(code, int):
        return code
    m = _HTTP_TEXT.search(_text_of(e))
    return int(m.group(1)) if m else None


def kind_for_http(code: int | None, message: str = "") -> str:
    """HTTP 코드 + 응답 메시지 → 종류. 설정 화면의 키 시험 판단(400/401/403=키, 429=한도)과 같은 기준."""
    if code is None:
        return NET
    if code in (401, 403):
        return BAD_KEY
    if code == 400 and _BAD_KEY_TEXT.search(message or ""):
        return BAD_KEY
    if code == 429:
        return QUOTA
    return HTTP


def kind_of(e) -> str:
    k = getattr(e, "kind", None)
    if k in KINDS:
        return k
    code = http_of(e)
    text = f"{_type_name(e)} {_text_of(e)}"
    if _NO_KEY_TEXT.search(text):
        return NO_KEY
    if _BAD_KEY_TEXT.search(text):
        return BAD_KEY
    if code in (401, 403):
        return BAD_KEY
    if code == 429 or _QUOTA_TEXT.search(text):
        return QUOTA
    if code is not None:
        return HTTP
    if _NET_TEXT.search(text):
        return NET
    return OTHER


def _clean(text: str, fallback: str) -> str:
    """한국어로 읽을 수 있는 부분만 남긴다. 절대 경로·Traceback·'RuntimeError:' 꼬리표는 지운다."""
    s = _EXC_PREFIX.sub("", text or "")
    s = _CLASS_TOKEN.sub("", s)                       # 문장 가운데 남은 예외 이름도 지운다
    s = _PATH_TEXT.sub("…", s)
    s = _TRACEBACK.sub("", s)
    s = re.sub(r"\(\s*[·/,-]*\s*\)", "", s)            # 이름을 지워 빈 괄호만 남은 곳
    s = re.sub(r"\s+", " ", s).strip(" :—-")
    if not s or not _HANGUL.search(s):
        return fallback
    return s[:300]


def reason(e) -> str:
    """주제 목록 등에 붙는 짧은 이유 — '키 없음'·'키가 맞지 않음(HTTP 400)'·'사용 한도(HTTP 429)'·'연결 실패'."""
    k, code = kind_of(e), http_of(e)
    text = _text_of(e)
    out = {"no_key": "키 없음", "bad_key": "키가 맞지 않음", "quota": "사용 한도",
           "net": ("Codex 연결 실패" if "codex" in text.lower() else "연결 실패"),
           "http": "요청을 처리하지 못함"}.get(k)
    if out is None:
        out = _clean(text, "점수를 매기지 못했습니다")[:80] or "점수를 매기지 못했습니다"
    elif code and k in (BAD_KEY, QUOTA, HTTP):
        out += f"(HTTP {code})"
    if "쉬는 중" in text:
        out += " — 잠시 쉬는 중"
    return out


def screen_text(e, fallback: str = DEFAULT_FALLBACK, *, kind: str | None = None, http: int | None = None) -> str:
    """화면에 그대로 보여 줄 한국어 한 줄. 예외 이름·Traceback·파일 경로는 들어가지 않는다."""
    k = kind if kind in KINDS else kind_of(e)
    code = http if isinstance(http, int) else http_of(e)
    text = _text_of(e)
    if k == NO_KEY:
        return "Codex ChatGPT 로그인이 필요합니다 — codex login status 로 확인하세요"
    if k == BAD_KEY:
        return "Codex ChatGPT 인증을 확인하세요 — 필요하면 codex login 으로 다시 로그인하세요"
    if k == QUOTA:
        if "하루 한도" in text:                       # TTS 하루 한도는 초기화 시각까지 알려 준다
            return text.split("HTTP", 1)[0].strip().rstrip(".")
        return "Codex 사용 한도에 걸렸습니다 — 잠시 뒤에 다시 해 보세요"
    if k == NET:
        who = "Codex " if "codex" in text.lower() else ""
        return f"{who}연결 실패 — 인터넷 연결을 확인하세요"
    if k == HTTP:
        return f"요청을 처리하지 못했습니다(HTTP {code})" if code else "요청을 처리하지 못했습니다"
    return _clean(text, fallback)


def mark(e, kind: str, http: int | None = None):
    """예외에 분류를 붙인다(다른 계층에서 던진 RuntimeError 를 그대로 다시 올릴 때)."""
    try:
        e.kind = kind if kind in KINDS else OTHER
        e.http = http
    except (AttributeError, TypeError):
        pass
    return e
