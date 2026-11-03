"""로컬 모델 파일(저장소 안 models/): 무엇이 필요한지, 있는지, 받는 일.

받는 곳은 sherpa-onnx GitHub 릴리스의 tar.bz2 다. 받는 중에는 models/_download/*.part 에 쓰고, 크기가 맞는지 확인한
뒤에 푼다. 풀 때 절대 경로·".."·링크·장치 파일이 든 압축은 거절한다. 이미 있으면 아무것도 하지 않는다.
환경변수 RECAP_SENSEVOICE_DIR, RECAP_SUPERTONIC_DIR 로 폴더를 바꿀 수 있다(그때는 그 폴더를 그대로 본다).
설정 화면의 받기 버튼은 start() 로 뒤에서 받고, 진행 상황은 status() 의 job 에 실린다.
"""
from __future__ import annotations

import os
import shutil
import tarfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MODELS_DIR = REPO_ROOT / "models"
DOWNLOAD_DIR = MODELS_DIR / "_download"
CHUNK = 1024 * 1024
SETUP_CMD = ".venv\\Scripts\\python scripts\\setup_models.py"

MODELS = [
    {"id": "sensevoice", "dir": "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17",
     "url": "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
            "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2",
     "size": 163_002_883, "files": ["model.int8.onnx", "tokens.txt"],
     "label": "받아쓰기(발음 확인)", "missing_effect": "없으면 발음 확인·어절 컷을 건너뜁니다",
     "env": "RECAP_SENSEVOICE_DIR"},
    {"id": "supertonic", "dir": "sherpa-onnx-supertonic-3-tts-int8-2026-05-11",
     "url": "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/"
            "sherpa-onnx-supertonic-3-tts-int8-2026-05-11.tar.bz2",
     "size": 128_774_318,
     "files": ["tts.json", "voice.bin", "unicode_indexer.bin", "duration_predictor.int8.onnx",
               "text_encoder.int8.onnx", "vector_estimator.int8.onnx", "vocoder.int8.onnx"],
     "label": "로컬 목소리(Supertonic)", "missing_effect": "기존 Supertonic 엔진을 선택할 때만 필요합니다(기본은 기존 Qwen 명령)",
     "env": "RECAP_SUPERTONIC_DIR"},
]


class ModelError(RuntimeError):
    """모델을 받거나 풀지 못한 이유."""


def spec(model_id: str) -> dict:
    for m in MODELS:
        if m["id"] == model_id:
            return m
    raise ModelError(f"모르는 모델입니다: {model_id}")


def model_dir(model_id: str) -> Path:
    """실제로 보는 폴더: 환경변수가 있으면 그 폴더, 없으면 저장소 안 models/<폴더 이름>."""
    m = spec(model_id)
    return Path(os.environ.get(m["env"]) or (MODELS_DIR / m["dir"]))


def ok(model_id: str) -> bool:
    """확인할 파일이 다 있는가."""
    d = model_dir(model_id)
    return all((d / f).is_file() for f in spec(model_id)["files"])


def _display_dir(model_id: str) -> str:
    d = model_dir(model_id)
    try:
        return d.resolve().relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:
        return spec(model_id)["dir"]


def env_dir(model_id: str) -> bool:
    """환경변수로 폴더를 정해 두었는가(그러면 앱에서 받지 않는다 — 받는 곳은 models/ 뿐)."""
    return bool(os.environ.get(spec(model_id)["env"]))


def status() -> list[dict]:
    out = []
    for m in MODELS:
        with _JOBS_LOCK:
            job = dict(_JOBS[m["id"]]) if m["id"] in _JOBS else None
        out.append({"id": m["id"], "label": m["label"], "ok": ok(m["id"]), "size_mb": round(m["size"] / 1_000_000),
                    "dir": _display_dir(m["id"]), "missing_effect": m["missing_effect"],
                    "env": env_dir(m["id"]), "job": job})
    return out


def setup_cmd() -> str:
    """없는 모델을 받는 명령(설정 화면에 보여 줌). 둘 다 없으면 --all, 로컬 목소리만 없으면 --supertonic."""
    missing = [m["id"] for m in MODELS if not ok(m["id"])]
    if "sensevoice" in missing and "supertonic" in missing:
        return SETUP_CMD + " --all"
    if missing == ["supertonic"]:
        return SETUP_CMD + " --supertonic"
    return SETUP_CMD


# ---------------------------------------------------------------- 안전한 풀기

def _check_members(tf: tarfile.TarFile, dest: Path) -> None:
    for m in tf.getmembers():
        name = m.name.replace("\\", "/")
        if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
            raise ModelError(f"압축 안에 절대 경로가 있습니다: {m.name}")
        if any(part == ".." for part in name.split("/")):
            raise ModelError(f"압축 안에 상위 폴더로 가는 경로가 있습니다: {m.name}")
        if m.issym() or m.islnk() or m.isdev() or m.isfifo():
            raise ModelError(f"압축 안에 링크·장치 파일이 있습니다: {m.name}")
        target = (dest / name).resolve()
        if dest.resolve() not in target.parents and target != dest.resolve():
            raise ModelError(f"압축 밖으로 푸는 경로가 있습니다: {m.name}")


def _extract(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:bz2") as tf:
        _check_members(tf, dest)
        try:
            tf.extractall(dest, filter="data")
        except TypeError:  # filter 인자가 없는 옛 파이썬 — _check_members 가 이미 막았다
            tf.extractall(dest)


# ---------------------------------------------------------------- 받기·복사

def _download(url: str, out: Path, size: int, progress, on_bytes=None) -> None:
    """out(*.part)에 받는다. 다 받은 크기가 size 와 다르면 .part 를 지우고 ModelError.
    on_bytes(받은 바이트, 전체 바이트)는 덩어리마다 부른다(설정 화면의 진행 막대)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    got = 0
    with urllib.request.urlopen(url, timeout=60) as r, open(out, "wb") as f:
        total = int(r.headers.get("Content-Length") or size)
        while True:
            buf = r.read(CHUNK)
            if not buf:
                break
            f.write(buf)
            step = got * 10 // max(total, 1)
            got += len(buf)
            if on_bytes:
                on_bytes(got, total)
            if got * 10 // max(total, 1) != step or got >= total:     # 10%마다 한 줄(1MB마다 찍으면 160줄이 넘는다)
                progress(f"받는 중 {got / 1_000_000:.1f} / {total / 1_000_000:.1f} MB")
    if got != size:
        out.unlink(missing_ok=True)
        raise ModelError(f"받은 크기가 다릅니다({got} 바이트, 기대 {size}) - 다시 받으세요")


def _copy_model(model_id: str, src_dir: Path, progress) -> None:
    m = spec(model_id)
    src = src_dir / m["dir"] if (src_dir / m["dir"]).is_dir() else src_dir
    missing = [f for f in m["files"] if not (src / f).is_file()]
    if missing:
        raise ModelError(f"{src} 에 모델 파일이 없습니다: {', '.join(missing)}")
    dst = model_dir(model_id)
    tmp = dst.parent / (dst.name + ".copying")
    if tmp.exists():
        shutil.rmtree(tmp)
    progress(f"복사 중 {src} → {dst}")
    shutil.copytree(src, tmp)
    if dst.exists():
        shutil.rmtree(dst)
    os.replace(tmp, dst)


def install(model_id: str, src_dir: str | Path | None = None, progress=print, on_bytes=None) -> Path:
    """모델 폴더를 갖춘다. 이미 있으면 그대로. src_dir 이 있으면 그 폴더에서 복사(네트워크 안 씀).
    없으면 tar.bz2 를 받아 models/ 에 풀고 확인한 뒤 받은 압축을 지운다. 돌려주는 값: 모델 폴더."""
    m = spec(model_id)
    d = model_dir(model_id)
    if ok(model_id):
        progress(f"{m['label']}: 이미 있음 - {d}")
        return d
    if src_dir is not None:
        _copy_model(model_id, Path(src_dir), progress)
    else:
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        archive = DOWNLOAD_DIR / Path(m["url"]).name
        part = archive.with_name(archive.name + ".part")
        progress(f"{m['label']}: 받기 시작 - {m['url']}")
        _download(m["url"], part, m["size"], progress, on_bytes)
        os.replace(part, archive)
        progress(f"{m['label']}: 푸는 중 ({archive.name})")
        _extract(archive, MODELS_DIR)
        archive.unlink(missing_ok=True)
        try:
            DOWNLOAD_DIR.rmdir()        # 비었으면 지운다(다른 모델을 받는 중이면 남는다)
        except OSError:
            pass
    if not ok(model_id):
        raise ModelError(f"{_display_dir(model_id)} 에 모델 파일이 갖춰지지 않았습니다: {', '.join(m['files'])}")
    progress(f"{m['label']}: 됨 - {d}")
    return d


# ---------------------------------------------------------------- 앱에서 받기(설정 화면의 받기 버튼)

_JOBS: dict[str, dict] = {}      # model_id -> {"state": downloading|extracting|done|error, "got", "total", "message"}
_JOBS_LOCK = threading.Lock()


def _set_job(model_id: str, **kw) -> None:
    with _JOBS_LOCK:
        _JOBS.setdefault(model_id, {}).update(kw)


def _run_job(model_id: str) -> None:
    m = spec(model_id)

    def on_bytes(got: int, total: int) -> None:
        _set_job(model_id, got=got, total=total, state="extracting" if got >= total else "downloading")

    try:
        install(model_id, progress=lambda _s: None, on_bytes=on_bytes)
        _set_job(model_id, state="done", got=m["size"], message="받았습니다")
    except ModelError as e:                 # 화면에 가는 문구 — 이 PC 의 절대 경로는 가린다
        from recap_shorts import errors
        _set_job(model_id, state="error", message=errors.screen_text(e, "받지 못했습니다 — 다시 받으세요"))
    except (urllib.error.URLError, OSError):
        _set_job(model_id, state="error", message="받지 못했습니다 — 인터넷 연결을 확인하고 다시 받으세요")
    except Exception as e:  # noqa: BLE001 — 뒤에서 도는 일이라 무엇이든 화면에 남긴다
        from recap_shorts import errors
        _set_job(model_id, state="error", message=f"받지 못했습니다 — {errors.screen_text(e, '알 수 없는 문제로 멈췄습니다')}")


def start(model_id: str) -> None:
    """뒤에서 받기 시작한다. 이미 있거나 받는 중이면 아무것도 하지 않는다.
    모르는 id·환경변수로 폴더를 정한 모델은 ModelError."""
    m = spec(model_id)
    if env_dir(model_id):
        raise ModelError(f"환경변수 {m['env']} 로 폴더를 정해 두어 여기서 받을 수 없습니다")
    if ok(model_id):
        return
    with _JOBS_LOCK:
        if _JOBS.get(model_id, {}).get("state") in ("downloading", "extracting"):
            return
        _JOBS[model_id] = {"state": "downloading", "got": 0, "total": m["size"], "message": ""}
    threading.Thread(target=_run_job, args=(model_id,), name=f"model-{model_id}", daemon=True).start()
