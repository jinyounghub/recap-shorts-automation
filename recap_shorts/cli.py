"""Codex-friendly local production commands. No web server or desktop app."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.parse import urlparse
import uuid

REPO = Path(__file__).resolve().parents[1]
PAUSED = 2  # successful checkpoint; inspect JSON status before resuming


def _read(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 객체가 필요합니다: {path}")
    return value


def _emit(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _job_dir(root: Path, job_id: str) -> Path:
    if not job_id or Path(job_id).name != job_id or job_id in (".", ".."):
        raise ValueError("올바른 작업 ID가 필요합니다")
    path = root / job_id
    if path.resolve().parent != root.resolve():
        raise ValueError("작업 폴더는 지정한 work 폴더 안에 있어야 합니다")
    if not (path / "state.json").is_file():
        raise FileNotFoundError(f"작업을 찾지 못했습니다: {job_id}")
    return path


@contextmanager
def work_lock(root: Path):
    """One writer per work directory; flock is released on crash/exit."""
    import fcntl

    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / ".recap.lock"
    if lock_path.is_symlink():
        raise ValueError("작업 잠금 파일은 심볼릭 링크일 수 없습니다")
    with lock_path.open("a+") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("같은 work 폴더에서 다른 제작 명령이 실행 중입니다") from None
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def source_from_url(url: str) -> dict:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("http 또는 https 게시글 URL을 지정하세요")
    if parsed.hostname in ("teamblind.com", "www.teamblind.com"):
        from recap_shorts.collect.blind import parse_blind_url
        alias, canonical = parse_blind_url(url)
        return {"site": "blind", "gallery": "best", "no": alias, "url": canonical, "minor": False}
    from recap_shorts.collect.dcinside import parse_dc_url, view_url
    gallery, no, minor = parse_dc_url(url)
    if no <= 0:
        raise ValueError("게시글 번호는 양수여야 합니다")
    return {"site": "dcinside", "gallery": gallery, "no": str(no),
            "minor": minor, "url": view_url(gallery, no, minor)}


def _snapshot(path: Path) -> dict:
    state = _read(path / "state.json")
    state["directory"] = str(path.resolve())
    return state


def doctor(*, check_asr: bool = True) -> dict:
    """Read-only preflight. Does not log in, load/download models or generate."""
    checks = []

    def add(name, ok, detail, *, required=True):
        checks.append({"name": name, "status": "ok" if ok else ("missing" if required else "check"),
                       "required": required, "detail": detail})

    add("python", sys.version_info >= (3, 11), sys.version.split()[0])
    for module in ("PIL", "numpy", "scipy", "requests", "soundfile", "lxml", "trafilatura", "playwright"):
        add(module, importlib.util.find_spec(module) is not None, f"Python import: {module}")
    for executable in ("ffmpeg", "ffprobe"):
        found = shutil.which(executable)
        add(executable, bool(found), found or "PATH에 없습니다. Homebrew FFmpeg 경로를 확인하세요")
    try:
        from recap_shorts.llm.codex import _environment, find_codex
        codex = find_codex()
        add("codex", True, codex)
        proc = subprocess.run([codex, "login", "status"], capture_output=True, text=True,
                              timeout=15, env=_environment(), shell=False)
        logged_in = proc.returncode == 0 and "chatgpt" in (proc.stdout + proc.stderr).lower()
        add("codex_chatgpt_login", logged_in,
            "기존 ChatGPT 로그인 확인" if logged_in else "codex login status로 확인 후 필요하면 codex login")
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        add("codex", False, str(exc) if not isinstance(exc, subprocess.TimeoutExpired) else "로그인 상태 확인 시간 초과")
    from recap_shorts import settings
    try:
        if settings.voice_engine() == "qwen":
            from recap_shorts.voice import qwen
            command = qwen.configured_command()
            add("local_tts", True, f"기존 로컬 실행 파일: {command[0]} (모델 실합성은 별도 확인)")
            using_bundled = not qwen.command_spec() and not os.environ.get("RECAP_QWEN_SCRIPT")
            if using_bundled:
                model = os.environ.get("RECAP_QWEN_MODEL", "").strip()
                local_path = bool(model) and (Path(model).is_absolute() or model.startswith((".", "~")))
                exists = Path(model).expanduser().is_dir() if local_path else bool(model)
                add("qwen_model", exists,
                    ("기존 로컬 모델 폴더 확인" if local_path and exists else
                     "기존 VoiceDesign 모델 경로/캐시 ID를 확인하세요. 캐시 ID의 실제 준비 여부는 합성 때 검증합니다. "
                     "MLX/Base는 기존 명령 연결을 사용하세요"))
        else:
            from recap_shorts import modelfiles
            add("local_tts", modelfiles.ok("supertonic"), "기존 RECAP_SUPERTONIC_DIR 모델 파일 확인")
            add("sherpa_onnx", importlib.util.find_spec("sherpa_onnx") is not None, "기존 로컬 ONNX 음성 런타임")
    except (RuntimeError, ImportError, OSError, ValueError) as exc:
        add("local_tts", False, str(exc))
    if check_asr:
        from recap_shorts import modelfiles
        add("local_asr", modelfiles.ok("sensevoice") and importlib.util.find_spec("sherpa_onnx") is not None,
            "기존 RECAP_SENSEVOICE_DIR + sherpa-onnx 확인. 명시적 --no-asr 사용 시 검사 생략")
    try:
        from recap_shorts.render.layout import load_font
        load_font(32)
        load_font(32, title=True)
        add("korean_fonts", True, "동봉 Pretendard 글꼴 읽기 성공")
    except (ImportError, RuntimeError, OSError) as exc:
        add("korean_fonts", False, str(exc))
    browser_path = os.environ.get("RECAP_BROWSER_PATH")
    channel = os.environ.get("RECAP_BROWSER_CHANNEL")
    if browser_path:
        add("capture_browser", Path(browser_path).expanduser().is_file(), "설정한 기존 브라우저 파일", required=False)
    elif channel:
        add("capture_browser", False, f"설치된 {channel}은 실제 캡처 때 확인합니다", required=False)
    elif importlib.util.find_spec("playwright"):
        add("capture_browser", False,
            "Playwright 설치 확인. 브라우저는 실제 캡처 때 확인합니다. 필요하면 python -m playwright install chromium",
            required=False)
    add("native_imagegen", False,
        "현재 Codex 세션의 내장 imagegen 도구로 생성합니다. CLI 진단은 도구 가용성/사용 한도를 확인하지 않습니다",
        required=False)
    return {"ready": not any(c["required"] and c["status"] != "ok" for c in checks),
            "scope": "local_preflight_only", "checks": checks}


def _execute(job, *, approve: bool = False, auto: bool = False) -> int:
    from recap_shorts.pipeline import CONFIRM_STEPS, Runner
    if auto:
        job.state["options"]["auto_steps"] = list(CONFIRM_STEPS)
    for step in job.state["steps"]:
        if step["status"] == "waiting":
            if approve or auto:
                step["status"] = "done"
                job.save()
            else:
                state = _snapshot(job.dir)
                state["next_action"] = "완료한 단계 산출물을 확인하고 resume JOB --approve로 진행하세요"
                _emit(state)
                return PAUSED
            break
    # awaiting_images is intentionally not approved here. The assets stage must
    # verify the actual files before it can finish, even with --auto/--approve.
    from recap_shorts.pipeline import JobStore
    runner = Runner(JobStore(job.root))
    runner.run_job(job)
    _emit(_snapshot(job.dir))
    return 0 if job.state["status"] == "done" else (1 if job.state["status"] == "error" else PAUSED)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="맥 Codex + 기존 로컬 모델로 만드는 한국어 쇼츠")
    p.add_argument("--work-root", default=os.environ.get("RECAP_WORK_ROOT", str(REPO / "work")))
    sub = p.add_subparsers(dest="command", required=True)
    diag = sub.add_parser("doctor", help="기존 도구/로그인/로컬 모델 경로 확인 (생성/설치 안 함)")
    diag.add_argument("--no-asr", action="store_true")
    create = sub.add_parser("create", help="게시글 URL 또는 로컬 topic JSON으로 작업 생성")
    source = create.add_mutually_exclusive_group(required=True)
    source.add_argument("--url")
    source.add_argument("--topic-file", type=Path)
    create.add_argument("--tts-engine", choices=("qwen", "supertonic"))
    create.add_argument("--no-asr", action="store_true")
    create.add_argument("--no-images", action="store_true")
    create.add_argument("--auto", action="store_true", help="완료 단계 확인을 자동 진행; 이미지 파일 대기는 유지")
    create.add_argument("--run", action="store_true")
    for name in ("run", "resume"):
        cmd = sub.add_parser(name, help="완료한 단계를 재사용하며 제작 진행")
        cmd.add_argument("job_id")
        cmd.add_argument("--approve", action="store_true", help="이미 완료한 확인 단계 진행")
        cmd.add_argument("--auto", action="store_true")
    for name in ("status", "image-tasks"):
        cmd = sub.add_parser(name)
        cmd.add_argument("job_id")
    sub.add_parser("list", help="로컬 작업 목록")
    return p


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    root = Path(args.work_root).expanduser().resolve()
    try:
        from recap_shorts import runtime
        runtime.apply()
        if args.command == "doctor":
            report = doctor(check_asr=not args.no_asr)
            _emit(report)
            return 0 if report["ready"] else 1
        if args.command == "list":
            jobs = []
            for path in sorted(root.glob("*/state.json"), reverse=True):
                if path.parent.resolve().parent == root:
                    st = _read(path)
                    jobs.append({k: st.get(k) for k in ("id", "title", "status", "progress", "created_at")})
            _emit({"work_root": str(root), "jobs": jobs})
            return 0
        if args.command in ("status", "image-tasks"):
            path = _job_dir(root, args.job_id)
            if args.command == "status":
                _emit(_snapshot(path))
            else:
                manifest = path / "assets" / "imagegen_tasks.json"
                result = _read(manifest) if manifest.is_file() else {"tasks": []}
                result["manifest"] = str(manifest)
                _emit(result)
            return 0
        with work_lock(root):
            from recap_shorts.pipeline import CONFIRM_STEPS, JobStore
            store = JobStore(root)
            if args.command == "create":
                topic = None
                if args.topic_file:
                    topic = _read(args.topic_file.expanduser())
                    if not isinstance(topic.get("source_text"), str) or not topic["source_text"].strip():
                        raise ValueError("topic JSON에는 비어 있지 않은 source_text가 필요합니다")
                    if topic.get("attachments"):
                        raise ValueError("--topic-file은 텍스트 입력용입니다. 첨부 캡처는 게시글 --url로 수집하세요")
                    source = {"site": "local", "gallery": "local", "no": uuid.uuid4().hex[:8],
                              "title": topic.get("topic", "로컬 주제"), "url": topic.get("source_url", "")}
                else:
                    source = source_from_url(args.url)
                options = {"auto_steps": list(CONFIRM_STEPS) if args.auto else [],
                           "check_asr": not args.no_asr, "no_image_gen": args.no_images}
                if args.tts_engine:
                    options["tts_engine"] = args.tts_engine
                job = store.create(source, options)
                if topic is not None:
                    (job.dir / "topic.json").write_text(json.dumps(topic, ensure_ascii=False, indent=2), encoding="utf-8")
                    for key in ("source", "read"):
                        job.update_step(key, status="done", progress=1.0, detail="로컬 topic JSON 입력")
                    job.artifact("topic", "topic.json")
                job.set_status("ready")
                if args.run:
                    return _execute(job)
                _emit(_snapshot(job.dir))
                return 0
            _job_dir(root, args.job_id)
            job = store.get(args.job_id)
            if job is None:
                raise ValueError("state.json을 읽을 수 없는 작업입니다")
            return _execute(job, approve=args.approve, auto=args.auto)
    except (ValueError, OSError, RuntimeError, ImportError) as exc:
        _emit({"status": "error", "error": str(exc)})
        return 1
    except KeyboardInterrupt:
        _emit({"status": "interrupted", "next_action": "같은 작업 ID로 resume을 실행하면 완료 단계를 재사용합니다"})
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
