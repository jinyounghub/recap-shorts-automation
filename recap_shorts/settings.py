"""Local configuration for Codex, existing TTS, sound effects and image assets.

- 모델 읽기 순서: 환경변수(앞뒤 공백 뺀 값이 비어 있지 않으면) → 설정 파일 → 없음("") 또는 기본 모델.
- 파일 쓰기는 임시 파일 + os.replace, 프로세스 안 threading.Lock(반쯤 쓴 파일이 남지 않게).
- 키 값은 어디에도 출력·로그·응답에 넣지 않는다(예외 메시지 포함).
- 경로는 부를 때마다 본다: 환경변수 RECAP_SETTINGS_FILE → config/settings.local.json.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FILE = REPO_ROOT / "config" / "settings.local.json"

# This fork uses the existing Codex ChatGPT login. API keys are never read.
IMAGE_ENGINES = [{"id": "codex", "label": "Codex 내장 imagegen"},
                 {"id": "off", "label": "생성 이미지 사용 안 함"}]
ENGINE_IDS = [e["id"] for e in IMAGE_ENGINES]
DEFAULT_ENGINE = "codex"
MODEL_ROLES = [{"role": "script", "label": "Codex 모델", "env": "RECAP_SCRIPT_MODEL", "default": ""}]
ROLE_IDS = [r["role"] for r in MODEL_ROLES]
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
VOICE_ENGINES = [{"id": "qwen", "label": "기존 로컬 Qwen TTS"},
                 {"id": "supertonic", "label": "기존 로컬 Supertonic"}]
VOICE_ENGINE_IDS = [e["id"] for e in VOICE_ENGINES]
SUPERTONIC_SIDS = list(range(10))
VOICE_FIELDS = ("engine", "sid")
# 효과음 폴더(설정 화면 '효과음'). 비어 있으면 config/fx.json 의 sfx.libraries(기본 꾸러미)를 쓴다.
# fill_missing = 고른 폴더에 없는 종류는 기본 꾸러미에서 채움(기본 켬).
SFX_FIELDS = ("dirs", "fill_missing")
SFX_MAX_DIRS = 8
# 짤 폴더(설정 화면 '짤'). 비어 있으면 config/fx.json 의 memes.library(기본 짤 꾸러미, 없으면 짤 없음).
MEME_FIELDS = ("dirs",)
MEME_MAX_DIRS = 8

_LOCK = threading.Lock()


class SettingsError(Exception):
    """설정 저장이 안 되는 이유. status 는 REST 코드(400: 값이 틀림, 409: 환경변수가 정함)."""
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def path() -> Path:
    return Path(os.environ.get("RECAP_SETTINGS_FILE") or DEFAULT_FILE)


def _load(p: Path | str | None = None) -> dict:
    """설정 파일 내용. 없거나 깨졌으면 빈 꼴. p 를 주면 그 파일(시험용)."""
    p = path() if p is None else Path(p)
    empty = {"image_engine": None, "models": {}, "voice": {}, "sfx": {}, "memes": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty
    if not isinstance(data, dict):
        return empty
    models, voice, sfx, memes = (data.get(k) for k in ("models", "voice", "sfx", "memes"))
    return {"image_engine": data.get("image_engine"),
            "models": models if isinstance(models, dict) else {}, "voice": voice if isinstance(voice, dict) else {},
            "sfx": sfx if isinstance(sfx, dict) else {}, "memes": memes if isinstance(memes, dict) else {}}


def image_engine() -> str:
    """Only native Codex imagegen or no generated images; legacy engines migrate to Codex."""
    v = os.environ.get("RECAP_IMAGE_ENGINE") or _load()["image_engine"]
    return v if v in ENGINE_IDS else DEFAULT_ENGINE


def _role(role: str) -> dict:
    for r in MODEL_ROLES:
        if r["role"] == role:
            return r
    raise KeyError(role)


def _model_env(role: str) -> str:
    return str(os.environ.get(_role(role)["env"], "")).strip()


def _model_file(role: str) -> str:
    v = _load()["models"].get(role)
    return v.strip() if isinstance(v, str) and MODEL_ID_RE.match(v.strip()) else ""


def model(role: str) -> str:
    """Optional Codex model override; an empty value preserves the current Codex model."""
    value = _model_env(role) or _model_file(role) or _role(role)["default"]
    if value.lower().startswith("gemini"):
        return ""
    return value


def model_source(role: str) -> str | None:
    """"env" | "file" | None(기본값)."""
    if _model_env(role):
        return "env"
    return "file" if _model_file(role) else None


def voice_engine() -> str:
    """Use the existing local Qwen environment by default; never a cloud TTS fallback."""
    value = os.environ.get("RECAP_TTS_ENGINE") or _load()["voice"].get("engine")
    return value if value in VOICE_ENGINE_IDS else "qwen"


def supertonic_sid() -> int:
    """로컬 목소리 번호 0~9: 환경변수 RECAP_TTS_SID → 설정 파일 → 0."""
    env = str(os.environ.get("RECAP_TTS_SID", "")).strip()
    if env.isdigit():
        return int(env)
    v = _load()["voice"].get("sid")
    return v if isinstance(v, int) and v in SUPERTONIC_SIDS else 0


def sfx_dirs() -> list[str]:
    """설정 화면에서 고른 효과음 폴더(절대 경로, 고른 순서). 없으면 [] — 그때는 config/fx.json 의 기본 꾸러미."""
    v = _load()["sfx"].get("dirs")
    out: list[str] = []
    for d in v if isinstance(v, list) else []:
        if isinstance(d, str) and d.strip() and d.strip() not in out:
            out.append(d.strip())
    return out[:SFX_MAX_DIRS]


def sfx_fill_missing() -> bool:
    """고른 폴더에 없는 종류를 기본 꾸러미에서 채울지(설정 파일, 없으면 True)."""
    v = _load()["sfx"].get("fill_missing")
    return v if isinstance(v, bool) else True


def _sfx_dir_path(d: str) -> Path:
    p = Path(os.path.expandvars(os.path.expanduser(d.strip().strip('"'))))
    return (p if p.is_absolute() else REPO_ROOT / p).resolve()


def _saved_paths(dirs: list[str]) -> set[str]:
    out = set()
    for d in dirs:
        try:
            out.add(str(_sfx_dir_path(d)))
        except (OSError, ValueError):
            pass
    return out


def _check_sfx(sfx: dict | None) -> list[str] | None:
    """효과음 설정 확인. 준 dirs 를 절대 경로 목록으로 바꿔 돌려준다(dirs 를 안 줬으면 None)."""
    if sfx is None:
        return None
    if not isinstance(sfx, dict):
        raise SettingsError("sfx 는 객체여야 합니다")
    if "fill_missing" in sfx and not isinstance(sfx["fill_missing"], bool):
        raise SettingsError("fill_missing 은 true 또는 false 입니다")
    if "dirs" not in sfx:
        return None
    dirs = sfx["dirs"]
    if dirs is None:
        return []
    if not isinstance(dirs, list) or not all(isinstance(d, str) for d in dirs):
        raise SettingsError("dirs 는 폴더 경로 목록이어야 합니다")
    from recap_shorts.fx import sfx as S
    saved = _saved_paths(sfx_dirs())
    out: list[str] = []
    for d in dirs:
        if not d.strip():
            continue
        if len(d) > 1024:
            raise SettingsError("폴더 경로가 너무 깁니다")
        p = _sfx_dir_path(d)
        if str(p) in saved:      # 이미 목록에 있던 폴더는 다시 보지 않는다 — 앱 밖에서 지운 폴더가 있어도 다른 폴더를 뺄 수 있게
            if str(p) not in out:
                out.append(str(p))
            continue
        if not p.is_dir():
            raise SettingsError(f"폴더가 없습니다: {d.strip()}")
        try:
            items, _, _ = S.folder_items(p, durations=False)
        except ValueError as e:
            raise SettingsError(str(e))
        if not items:
            raise SettingsError(f"이 폴더에서 효과음을 찾지 못했습니다: {d.strip()} — 파일 이름을 종류_번호(예: pop_01.wav)로 하거나 "
                                "종류 이름 폴더(예: pop\\)에 넣으세요")
        if str(p) not in out:
            out.append(str(p))
    if len(out) > SFX_MAX_DIRS:
        raise SettingsError(f"효과음 폴더는 {SFX_MAX_DIRS}개까지입니다")
    return out


def _pack_view(p: Path) -> tuple[str | None, dict | None]:
    """폴더가 분석 꾸러미(fx/library.py)면 (원본 폴더 경로, {"kind","id"})."""
    from recap_shorts.fx import library
    pk = library.pack_of(p)
    return (pk["source_dir"], {"kind": pk["kind"], "id": pk["id"]}) if pk else (None, None)


def _sfx_folder_view(p: Path, shown: str) -> dict:
    from recap_shorts.fx import sfx as S
    err = None
    try:
        items, skipped, has_mf = S.folder_items(p, durations=False)
    except ValueError as e:
        items, skipped, has_mf, err = [], 0, True, str(e)
    types: dict[str, int] = {}
    for it in items:
        types[it["type"]] = types.get(it["type"], 0) + 1
    label, pack = _pack_view(p)
    return {"path": shown, "label": label, "pack": pack, "exists": p.is_dir(), "count": len(items), "skipped": skipped,
            "manifest": has_mf, "types": dict(sorted(types.items())), "error": err}


def meme_dirs() -> list[str]:
    """설정 화면에서 고른 짤 폴더(절대 경로, 고른 순서). 없으면 [] — 그때는 config/fx.json 의 memes.library."""
    v = _load()["memes"].get("dirs")
    out: list[str] = []
    for d in v if isinstance(v, list) else []:
        if isinstance(d, str) and d.strip() and d.strip() not in out:
            out.append(d.strip())
    return out[:MEME_MAX_DIRS]


def _check_memes(memes: dict | None) -> list[str] | None:
    """짤 설정 확인. 준 dirs 를 절대 경로 목록으로(안 줬으면 None). 폴더에 manifest.json 이나 그림이 있어야 한다."""
    if memes is None:
        return None
    if not isinstance(memes, dict):
        raise SettingsError("memes 는 객체여야 합니다")
    if "dirs" not in memes:
        return None
    dirs = memes["dirs"]
    if dirs is None:
        return []
    if not isinstance(dirs, list) or not all(isinstance(d, str) for d in dirs):
        raise SettingsError("dirs 는 폴더 경로 목록이어야 합니다")
    from recap_shorts.fx import memes as M
    saved = _saved_paths(meme_dirs())
    out: list[str] = []
    for d in dirs:
        if not d.strip():
            continue
        if len(d) > 1024:
            raise SettingsError("폴더 경로가 너무 깁니다")
        p = _sfx_dir_path(d)
        if str(p) in saved:      # 효과음과 같음 — 이미 있던 폴더는 없어졌어도 그대로 둔다(화면에 '폴더가 없습니다'로 보임)
            if str(p) not in out:
                out.append(str(p))
            continue
        if not p.is_dir():
            raise SettingsError(f"폴더가 없습니다: {d.strip()}")
        items, note = M.load_folder(p)
        if not items:
            raise SettingsError(f"이 폴더에서 짤을 찾지 못했습니다: {d.strip()}" + (f" — {note}" if note else ""))
        if str(p) not in out:
            out.append(str(p))
    if len(out) > MEME_MAX_DIRS:
        raise SettingsError(f"짤 폴더는 {MEME_MAX_DIRS}개까지입니다")
    return out


def _meme_folder_view(p: Path, shown: str) -> dict:
    from recap_shorts.fx import memes as M
    items, note = M.load_folder(p) if p.is_dir() else ([], "")
    label, pack = _pack_view(p)
    return {"path": shown, "label": label, "pack": pack, "exists": p.is_dir(), "count": len(items),
            "manifest": (p / "manifest.json").is_file(), "error": note if (p.is_dir() and not items and note) else None}


def memes_view() -> dict:
    """설정 화면의 '짤' 묶음: 고른 폴더, 기본 짤 꾸러미(없을 수 있음)."""
    from recap_shorts.fx.config import load_config, resolve_path
    dirs = meme_dirs()
    lib = str((load_config().get("memes") or {}).get("library") or "")
    default = [_meme_folder_view(resolve_path(lib), lib)] if lib else []
    using = "custom" if dirs else ("default" if any(f["count"] for f in default) else "none")
    return {"using": using, "dirs": [_meme_folder_view(Path(d), d) for d in dirs], "default": default,
            "max_dirs": MEME_MAX_DIRS}


def sfx_view() -> dict:
    """설정 화면의 '효과음' 묶음: 고른 폴더와 기본 꾸러미 각각의 효과음 수·종류."""
    from recap_shorts.fx import sfx as S
    from recap_shorts.fx.config import load_config, resolve_path
    dirs = sfx_dirs()
    default = [str(d) for d in ((load_config().get("sfx") or {}).get("libraries") or [])]
    return {"using": "custom" if dirs else "default", "fill_missing": sfx_fill_missing(),
            "dirs": [_sfx_folder_view(Path(d), d) for d in dirs],
            "default": [_sfx_folder_view(resolve_path(d), d) for d in default],
            "known_types": S.KNOWN_TYPES, "max_dirs": SFX_MAX_DIRS}


def _check_models(models: dict | None) -> None:
    if models is None:
        return
    if not isinstance(models, dict):
        raise SettingsError("models 는 객체여야 합니다")
    for role, v in models.items():
        if role not in ROLE_IDS:
            raise SettingsError(f"모르는 모델 자리입니다: {role}")
        if v in (None, ""):
            continue
        if not isinstance(v, str) or not MODEL_ID_RE.match(v.strip()):
            raise SettingsError("모델 이름에 쓸 수 없는 글자가 있습니다")
        if _model_env(role):
            raise SettingsError(f"환경변수 {_role(role)['env']} 로 정해져 있어 여기서 바꿀 수 없습니다", status=409)


def _check_voice(voice: dict | None) -> None:
    if voice is None:
        return
    if not isinstance(voice, dict):
        raise SettingsError("voice 는 객체여야 합니다")
    if "engine" in voice:
        if voice["engine"] not in VOICE_ENGINE_IDS:
            raise SettingsError("모르는 목소리 엔진입니다")
        if voice["engine"] == "supertonic":
            from recap_shorts import modelfiles
            if not modelfiles.ok("supertonic"):
                raise SettingsError("기존 로컬 Supertonic 모델 경로를 RECAP_SUPERTONIC_DIR 로 연결하세요")
    if "sid" in voice:
        if not isinstance(voice["sid"], int) or isinstance(voice["sid"], bool) or voice["sid"] not in SUPERTONIC_SIDS:
            raise SettingsError("목소리 번호는 0~9 입니다")
        if str(os.environ.get("RECAP_TTS_SID", "")).strip():
            raise SettingsError("환경변수 RECAP_TTS_SID 로 정해져 있어 여기서 바꿀 수 없습니다", status=409)


def save(keys: dict | None = None, image_engine: str | None = None, models: dict | None = None,
         voice: dict | None = None, sfx: dict | None = None, memes: dict | None = None) -> dict:
    """keys·models 에 없는 이름은 그대로 두고, "" 또는 null 은 설정 파일에서 지운다(모델은 기본으로 돌아감).
    voice = {"engine": qwen|supertonic, "sid": 0~9} 중 준 것만 바꾼다.
    sfx = {"dirs": [폴더…] (빈 목록·null = 기본 꾸러미로), "fill_missing": bool} 중 준 것만 바꾼다.
    memes = {"dirs": [폴더…] (빈 목록·null = 기본 짤로)}.
    환경변수가 정하고 있는 값을 바꾸려 하면 SettingsError(409), 값·이름이 틀리면 SettingsError(400)."""
    if keys:
        raise SettingsError("API 키 설정은 지원하지 않습니다. 기존 Codex ChatGPT 로그인을 사용하세요")
    if image_engine is not None and image_engine not in ENGINE_IDS:
        raise SettingsError("모르는 image_engine 입니다")
    _check_models(models)
    _check_voice(voice)
    sfx_dirs_new = _check_sfx(sfx)
    meme_dirs_new = _check_memes(memes)
    with _LOCK:
        data = _load()
        if image_engine is not None:
            data["image_engine"] = image_engine
        mods = {r: v for r, v in data["models"].items() if r in ROLE_IDS}
        for role, v in (models or {}).items():
            if v in (None, ""):
                mods.pop(role, None)
            else:
                mods[role] = v.strip()
        vo = {k: v for k, v in data["voice"].items() if k in VOICE_FIELDS}
        vo.update({k: v for k, v in (voice or {}).items() if k in VOICE_FIELDS})
        sx = {k: v for k, v in data["sfx"].items() if k in SFX_FIELDS}
        if sfx_dirs_new is not None:
            if sfx_dirs_new:
                sx["dirs"] = sfx_dirs_new
            else:
                sx.pop("dirs", None)
        if isinstance((sfx or {}).get("fill_missing"), bool):
            sx["fill_missing"] = sfx["fill_missing"]
        mm = {k: v for k, v in data["memes"].items() if k in MEME_FIELDS}
        if meme_dirs_new is not None:
            if meme_dirs_new:
                mm["dirs"] = meme_dirs_new
            else:
                mm.pop("dirs", None)
        out = {}
        if data["image_engine"] is not None:
            out["image_engine"] = data["image_engine"]
        if mods:
            out["models"] = mods
        if vo:
            out["voice"] = vo
        if sx:
            out["sfx"] = sx
        if mm:
            out["memes"] = mm
        p = path()
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=p.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(tmp, p)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    return public_view()


def public_view() -> dict:
    """Non-secret local configuration for CLI and library helpers."""
    return {"file": str(path()), "auth": "codex_chatgpt", "keys": [], "groups": [],
            "image_engine": image_engine(), "image_engines": IMAGE_ENGINES,
            "model_roles": [{**r, "value": model(r["role"]), "source": model_source(r["role"])}
                            for r in MODEL_ROLES],
            "voice": {"engine": voice_engine(), "engines": VOICE_ENGINES,
                      "sid": supertonic_sid(), "sids": SUPERTONIC_SIDS},
            "sfx": sfx_view(), "memes": memes_view()}
