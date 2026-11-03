"""v2 단계 실행기: job 하나를 source → read → script → voice → assets → scenes → render → review 로 돌리고
work/<job_id>/state.json 을 계속 갱신한다 (형식·단계: docs/03_v2_app_design.md 1.1, 1.2).

- 사람 확인 단계(read·script·scenes·review)는 일을 마친 뒤 status="waiting" 으로 멈추고, continue 로 진행한다.
  options.auto_steps(또는 auto_topic=True)에 든 단계는 멈추지 않고 지나간다.
- 재개: 이미 done 인 단계는 건너뛰고, error 단계는 continue 로 그 단계부터 다시 한다.
- 바깥 세계(커뮤니티, Codex, native imagegen, 로컬 목소리, 렌더)는 모두 Deps 를 거친다 — 시험은 Deps 를 가짜로 바꿔 끼운다.
- 닉네임·IP 는 저장하지 않는다(collect 모듈이 이미 지킴). 화면에 출처 줄·글 제목 카드·배경음악을 넣지 않는다.
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import threading
import time
import traceback
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from recap_shorts import errors
from recap_shorts.images.codex_imagegen import PendingImages

ROOT = Path(__file__).resolve().parents[1]
WORK_ROOT = Path(os.environ.get("RECAP_WORK_ROOT", ROOT / "work"))
ERRORS_LOG = "_logs/errors.log"     # 화면에 안 보이는 자세한 기록(Traceback). 작업 폴더 안에만 둔다.

STEPS = [("source", "글 가져오기"), ("read", "자료 읽기"), ("script", "대본"), ("voice", "목소리"),
         ("assets", "자료 화면"), ("scenes", "장면 배치"), ("render", "영상 만들기"), ("review", "검수")]
STEP_KEYS = [k for k, _ in STEPS]
CONFIRM_STEPS = ("read", "script", "scenes", "review")   # 사람이 확인하는 단계
GALLERY_NAMES = {"baseball_new13": "국내야구", "dcbest": "실시간 베스트"}


def site_of(source: dict) -> str:
    """명시적인 로컬 자료 출처를 보존하고, 출처가 없는 예전 작업만 디시로 읽는다."""
    site = str((source or {}).get("site") or "dcinside").strip().lower()
    return site if site in ("blind", "local") else "dcinside"


def post_label(source: dict) -> str:
    """'디시인사이드 국내야구 갤러리' / '블라인드 블라블라 토픽' — 글이 어디서 왔는지 붙이는 이름."""
    site = site_of(source)
    if site == "local":
        return "사용자 제공 로컬 자료"
    if site == "blind":
        return f"블라인드 {source.get('topic') or '블라인드'} 토픽"
    return f"디시인사이드 {GALLERY_NAMES.get(source.get('gallery'), source.get('gallery'))} 갤러리"


def _safe_name(s: str) -> str:
    """폴더·파일 이름에 쓸 수 없는 글자를 빼고 길이를 줄인다(블라인드 토픽 이름 등)."""
    out = re.sub(r'[\\/:*?"<>|\s]+', "_", str(s or "")).strip("._-")
    return out[:40] or "x"


DEFAULT_OPTIONS = {
    "auto_steps": [],            # 멈추지 않고 지나갈 확인 단계 (예: ["read"])
    "tts_engine": None,          # None = 로컬 설정의 목소리 엔진(qwen | local | supertonic)
    "check_asr": True,
    "script_llm": None, "script_model": None, "refine": True,
    "no_image_gen": False, "max_ai_images": 12, "ai_workers": 2,
    "reuse_images_from": None,   # 폴더: AI 이미지를 새로 만드는 대신 여기 png 를 재사용
    "image_gen_limit": None,     # 실제로 새로 만들 장 수(나머지는 reuse 로)
    "max_attachments": 8,
    "entity_images": True,       # 대본이 말하는 대상(캐릭터·작품 등)의 실제 사진을 찾아 그 문장에 보여 줌
    "sensitive_treatment": True, # 피·부상·시신·선정적인 그림을 판정해 흑백/흐림 처리(노란 딱지 피하기). False 면 호출도 처리도 없음. config/sensitive.json
    "max_ai_share": 0.34,        # AI 그림 컷 비율 상한(화면은 글·댓글 캡처 위주, AI 그림은 보조)
    "clef_scene_fit": False,     # 끔: 그림마다 Clef 를 불러 문장 적합도를 묻던 것. 캡처 위주 규칙에서는 18컷 중 17컷이
                                 # 같아(효과 거의 없음) Clef 하루치(1만 뉴런)의 대부분만 썼다. 켜려면 옵션에 True.
}
PROFANITY = re.compile(r"\S*(좆|씨발|시발|병신|지랄|ㅅㅂ|ㅂㅅ|새끼|새기|등신|개새)\S*")
CAPTURE_WORDS = ("캡처", "실제 사진 필요", "스크린샷")


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def clean_text(s: str) -> str:
    """욕설 낱말을 뺀다(원문 대본 근거로 쓰이는 글이라 모델이 따라 쓰지 않게)."""
    return re.sub(r"[ \t]{2,}", " ", PROFANITY.sub("", s or "")).strip()


# ------------------------------------------------------------------ 바깥 세계
def _clef(module: str, name: str, *args, **kw):
    """Legacy optional score hook. Cloudflare calls have been removed."""
    return None


class Deps:
    """실제 구현. 시험에서는 필요한 메서드만 바꿔 끼운다."""

    # 커뮤니티 글(디시인사이드·블라인드)
    def http_client(self, job_dir):
        from recap_shorts.collect.http import HttpClient
        return HttpClient(job_dir)

    def get_post(self, client, gallery: str, no, minor: bool, site: str = "dcinside"):
        if site == "blind":
            from recap_shorts.collect import blind
            return blind.get_post(gallery, str(no), bool(minor), client=client)
        from recap_shorts.collect import dcinside
        return dcinside.get_post(gallery, int(no), minor, client=client)

    def download_image(self, client, url: str, dest_noext: Path, referer: str) -> Path:
        import io
        from PIL import Image
        from recap_shorts.collect.http import Blocked, BlockedError
        client.session.headers["Referer"] = referer
        r = client.fetch(url, check_robots=False, use_cache=False)
        if isinstance(r, Blocked):
            raise BlockedError(r)
        with Image.open(io.BytesIO(r.content)) as im:
            fmt = (im.format or "").upper()
            ext = {"JPEG": ".jpg", "PNG": ".png"}.get(fmt)
            if ext:
                dest = dest_noext.with_suffix(ext)
                dest.write_bytes(r.content)
            else:                                   # GIF/WEBP 등은 첫 장면을 PNG 로
                dest = dest_noext.with_suffix(".png")
                im.convert("RGB").save(dest)
        return dest

    def screenshot_post(self, client, url: str, out_png: Path, site: str = "dcinside"):
        if site == "blind":
            from recap_shorts.collect import blind
            return blind.screenshot_post(url, out_png, client=client)
        from recap_shorts.collect import dcinside
        return dcinside.screenshot_post(url, out_png, client=client)

    # Codex OAuth
    def read_image(self, images: list, instruction: str) -> dict:
        from recap_shorts.llm.codex import read_images
        return read_images(images, instruction, json=True)

    def locate_regions(self, path: Path, wants: list[str]) -> dict:
        from recap_shorts.images.regions import locate
        return locate(path, wants)

    def script_llm(self, kind=None, model=None):
        from recap_shorts.llm import make_script_llm
        return make_script_llm(kind, model)

    def hint_prompts(self, hints: list[str]) -> list[str]:
        from recap_shorts.images.provider import hints_to_prompts
        return hints_to_prompts(hints)

    # 이미지 서버
    def image_available(self) -> bool:
        from recap_shorts.images import provider
        return provider.available()

    def generate_image(self, prompt: str, out_png: Path) -> dict:
        from recap_shorts.images import provider
        return provider.generate(prompt, out_png)

    # 목소리·렌더
    def make_voice(self, script: dict, out_dir: Path, engine: str, check_asr: bool) -> dict:
        from recap_shorts.voice.pipeline import make_voice
        return make_voice(script, out_dir, engine=engine, check_asr=check_asr)

    def redo_voice(self, voice_dir: Path, index: int, variants: list[tuple[str, str]]) -> dict:
        from recap_shorts.voice.redo import redo
        return redo(voice_dir, index, variants)

    def render(self, timeline: Path, out_mp4: Path, progress=None) -> dict:
        from recap_shorts.render.render import render
        return render(str(timeline), str(out_mp4), quiet=True, progress=progress)

    # Clef-omni (없거나 실패하면 None)
    def clef_inspect(self, path: Path, kind: str):
        return _clef("inspect", "inspect_image", path, kind)

    def image_quality(self, paths: list[Path], hints: list[str]) -> dict:
        """AI 이미지 결함 검사(Codex 비전 1회). 시험 중에는 실제 API 를 부르지 않는다."""
        if "PYTEST_CURRENT_TEST" in os.environ:
            return {"items": [], "worst": []}
        from recap_shorts.images import quality
        return quality.review(paths, hints)

    def sensitive_read(self, images: list, instruction: str) -> dict:
        """그림 ≤8장의 자극·잔혹 정도 판정(Codex 비전 1회, thinking low). 시험 중에는 실제 API 를 부르지 않는다."""
        if "PYTEST_CURRENT_TEST" in os.environ:
            raise RuntimeError("시험 중에는 실제 API 를 부르지 않음")
        from recap_shorts.llm.codex import read_images
        return read_images(images, instruction, json=True, thinking="low")

    def entity_images(self, job_dir: Path, script: dict, timings: dict, topic: dict) -> dict:
        """대본이 말하는 대상의 사진 찾기(Codex 텍스트 1회 + 대상당 비전 ≤2회). 시험 중에는 아무것도 하지 않는다."""
        if "PYTEST_CURRENT_TEST" in os.environ:
            return {"entries": [], "found": [], "missed": [], "extracted": [], "calls": 0}
        from recap_shorts.images import entities
        return entities.find_entity_images(job_dir, script, timings, topic, self.http_client(job_dir))

    def pick_panels(self, src: Path, boxes: list, sentences: list[dict]) -> dict:
        """칸 쌓기 그림에서 문장별로 쓸 칸 고르기(Codex 비전 1회). 시험 중에는 실제 API 를 부르지 않는다."""
        if "PYTEST_CURRENT_TEST" in os.environ:
            return {"picks": []}
        from recap_shorts.images.regions import pick_panels
        return pick_panels(src, boxes, sentences)

    def clef_fit_matrix(self, sentences: list[str], images: list[Path]):
        return _clef("match", "fit_matrix", sentences, [str(p) for p in images])

    def clef_watch(self, mp4: Path, topic_line: str):
        return _clef("inspect", "watch_video", mp4, topic_line)

    def clef_triage(self, posts: list[dict]):
        return _clef("triage", "triage", posts)

    def topic_fit_llm(self, prompt: str, model: str) -> dict:
        """채널 대주제 맞춤 점수용 Codex 텍스트 1회(JSON 객체). 시험 중에는 실제 API 를 부르지 않고 예외를 낸다."""
        from recap_shorts.collect.topicfit import codex_llm
        return codex_llm(prompt, model)


# ------------------------------------------------------------------ job 상태
class Job:
    def __init__(self, root: Path, state: dict, dir: Path | None = None):
        self.root = Path(root)
        self.dir = Path(dir) if dir else self.root / state["id"]   # 명시적으로 지정한 로컬 작업 폴더
        self.state = state
        self.lock = threading.RLock()
        self.readonly = False        # 명시적으로 읽기 전용으로 연 작업은 다시 쓰지 않는다

    @property
    def archived(self) -> bool:
        return (self.state.get("archive") or {}).get("status") == "done"

    @property
    def id(self) -> str:
        return self.state["id"]

    @property
    def options(self) -> dict:
        return {**DEFAULT_OPTIONS, **(self.state.get("options") or {})}

    def step(self, key: str) -> dict:
        return next(s for s in self.state["steps"] if s["key"] == key)

    def save(self) -> None:
        with self.lock:
            if self.readonly:
                return
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.dir / "state.json.tmp"
            tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")
            for k in range(5):
                try:
                    os.replace(tmp, self.dir / "state.json")
                    return
                except PermissionError:
                    time.sleep(0.05 * (k + 1))
            (self.dir / "state.json").write_text(tmp.read_text(encoding="utf-8"), encoding="utf-8")

    def log(self, msg: str) -> None:
        with self.lock:
            self.state["log"].append({"t": now(), "msg": msg})
            del self.state["log"][:-500]
            self.save()

    def update_step(self, key: str, **kw) -> None:
        with self.lock:
            self.step(key).update(kw)
            self.save()

    def set_sub(self, key: str, label: str, status: str, note: str | None = None) -> None:
        with self.lock:
            sub = self.step(key).setdefault("sub", [])
            item = next((x for x in sub if x["label"] == label), None)
            if item is None:
                item = {"label": label, "status": status, "note": note}
                sub.append(item)
            item.update(status=status, note=note)
            self.save()

    def set_status(self, status: str) -> None:
        with self.lock:
            self.state["status"] = status
            self.state["progress"] = self.overall_progress()
            self.save()

    def overall_progress(self) -> float:
        ps = [1.0 if s["status"] in ("done", "waiting") else (s.get("progress") or 0.0)
              for s in self.state["steps"]]
        return round(sum(ps) / len(ps), 3)

    def artifact(self, key: str, value) -> None:
        with self.lock:
            self.state["artifacts"][key] = value
            self.save()

    def reset_from(self, key: str) -> None:
        """key 단계와 그 뒤 단계를 todo 로 되돌리고, 그 결과물(영상·설명 문구)을 지운다."""
        with self.lock:
            hit = False
            for s in self.state["steps"]:
                if s["key"] == key:
                    hit = True
                if hit:
                    s.update(status="todo", progress=0.0, detail="", sub=[], started_at=None, ended_at=None, error=None)
            arts = self.state["artifacts"]
            order = STEP_KEYS.index(key)
            if order <= STEP_KEYS.index("review"):
                arts.pop("description", None)
            if order <= STEP_KEYS.index("render"):
                arts.pop("video", None)
                arts.pop("upload_sound", None)
            if order <= STEP_KEYS.index("voice"):
                arts.pop("voice", None)
            self.save()


class JobStore:
    def __init__(self, root: Path | str | None = None):
        self.root = Path(root or WORK_ROOT)
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        self._load_existing()

    def _load_existing(self) -> None:
        if not self.root.is_dir():
            return
        for p in self.root.glob("*/state.json"):
            try:
                st = json.loads(p.read_text(encoding="utf-8"))
                if "steps" not in st or "id" not in st or st["id"] != p.parent.name:
                    continue
            except (OSError, ValueError):
                continue
            job = Job(self.root, st)
            st.setdefault("source", {})["site"] = site_of(st.get("source"))   # site 가 없던 예전 작업은 디시로 채워 화면에 보인다
            if st.get("status") == "running":      # 서버가 도중에 꺼졌음 → 이어 하려면 continue
                for s in st["steps"]:
                    if s["status"] == "running":
                        s.update(status="error", error="서버가 다시 시작되어 중단됨 — continue 로 이어서 합니다")
                st["status"] = "error"
            self.jobs[st["id"]] = job

    def create(self, source: dict, options: dict | None = None) -> Job:
        src = dict(source)
        src["site"] = site_of(src)
        jid = f"{datetime.now():%Y%m%d-%H%M%S-%f}_{_safe_name(src['gallery'])}_{_safe_name(src['no'])}"
        # Never reuse an existing job folder, including repeated CLI submissions.
        base_id, collision = jid, 0
        while (self.root / jid).exists():
            collision += 1
            jid = f"{base_id}_{collision}"
        opts = dict(options or {})
        if opts.pop("auto_topic", False):
            opts["auto_steps"] = sorted(set(opts.get("auto_steps") or []) | {"read"})
        state = {
            "id": jid, "title": src.get("title") or f"{src['gallery']} {src['no']}",
            "source": {k: src.get(k) for k in ("site", "gallery", "no", "url", "title", "minor", "topic")},
            "created_at": now(), "status": "running", "progress": 0.0, "options": opts,
            "steps": [{"key": k, "label": lab, "status": "todo", "progress": 0.0, "detail": "", "sub": [],
                       "started_at": None, "ended_at": None, "error": None} for k, lab in STEPS],
            "artifacts": {}, "log": [],
        }
        job = Job(self.root, state)
        job.dir.mkdir(parents=True, exist_ok=True)
        job.save()
        with self.lock:
            self.jobs[jid] = job
        return job

    def get(self, jid: str) -> Job | None:
        return self.jobs.get(jid)

    def all(self) -> list[Job]:
        return sorted(self.jobs.values(), key=lambda j: j.state["created_at"], reverse=True)


# ------------------------------------------------------------------ 단계 컨텍스트
class Ctx:
    def __init__(self, job: Job, key: str, deps: Deps):
        self.job, self.key, self.deps = job, key, deps
        self.dir = job.dir
        self.opt = job.options

    def progress(self, p: float, detail: str | None = None) -> None:
        kw = {"progress": round(max(0.0, min(1.0, p)), 3)}
        if detail is not None:
            kw["detail"] = detail
        self.job.update_step(self.key, **kw)
        self.job.set_status(self.job.state["status"])

    def sub(self, label: str, status: str, note: str | None = None) -> None:
        self.job.set_sub(self.key, label, status, note)

    def log(self, msg: str) -> None:
        self.job.log(f"[{self.key}] {msg}")

    def read_json(self, rel: str):
        return json.loads((self.dir / rel).read_text(encoding="utf-8"))

    def write_json(self, rel: str, obj) -> Path:
        p = self.dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
        return p


def rel(job: Job, p: Path | str) -> str:
    return Path(p).resolve().relative_to(job.dir.resolve()).as_posix()


# ------------------------------------------------------------------ 단계: source
def write_error_log(job: Job, where: str, exc: BaseException | None = None, text: str | None = None) -> bool:
    """화면에 안 보이는 자세한 기록(Traceback·예외 이름·경로)을 <작업 폴더>/_logs/errors.log 에 덧쓴다.
    실패해도 화면 흐름을 막지 않는다(보관된 job 은 읽기 전용)."""
    try:
        p = job.dir / ERRORS_LOG
        p.parent.mkdir(parents=True, exist_ok=True)
        body = text if text is not None else "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)) \
            if exc is not None else ""
        with p.open("a", encoding="utf-8") as f:
            f.write(f"\n===== {now()} · {job.id} · {where} =====\n{body.rstrip()}\n")
        return True
    except Exception:  # noqa: BLE001
        return False


def report_error(job: Job, where: str, e: BaseException) -> str:
    """Traceback 은 _logs/errors.log 에만 남기고, 화면에 보여 줄 한국어 한 줄을 돌려준다.
    예외 이름·Traceback·이 PC 의 파일 경로는 화면 문구에 넣지 않는다."""
    text = errors.screen_text(e)
    if write_error_log(job, where, e):
        text += f" — 자세한 내용은 작업 폴더의 {ERRORS_LOG} 에 남겼습니다"
    return text


def step_source(c: Ctx) -> None:
    src = c.job.state["source"]
    site = site_of(src)
    gallery, no, minor = src["gallery"], src["no"], bool(src.get("minor"))
    client = c.deps.http_client(c.dir)
    where = "블라인드" if site == "blind" else "디시"
    c.progress(0.05, f"{where} 글 본문을 가져오는 중")
    c.sub("글 본문", "running")
    post = c.deps.get_post(client, gallery, no, minor, site=site)
    c.write_json(f"post/post_{no}.json", asdict(post))
    c.sub("글 본문", "done", f"추천 {post.recommends} · 조회 {post.views}")
    topic_name = getattr(post, "topic", "") or src.get("topic") or ""
    c.job.state["source"].update(url=post.url, title=post.title, written_at=post.written_at,
                                 views=post.views, recommends=post.recommends)
    if topic_name:
        c.job.state["source"]["topic"] = topic_name
    c.job.state["title"] = post.title
    c.job.artifact("post", f"post/post_{no}.json")
    urls = post.images[: int(c.opt["max_attachments"])]
    c.sub("첨부 이미지", "running", f"0/{len(urls)}")
    got, failed = [], []
    for k, u in enumerate(urls, 1):
        c.progress(0.15 + 0.5 * (k - 1) / max(1, len(urls)), f"첨부 이미지 {k}/{len(urls)} 받는 중")
        try:
            p = c.deps.download_image(client, u, c.dir / "post" / f"img_{k}", post.url)
            got.append(rel(c.job, p))
        except Exception as e:  # noqa: BLE001
            failed.append(k)
            c.log(f"첨부 이미지 {k} 받기 실패: {errors.reason(e)}")
    if urls and not got:
        raise RuntimeError("첨부 이미지를 하나도 받지 못했습니다(주소 만료·차단 가능)")
    c.write_json("post/attachments.json", {"files": got, "failed": failed})
    c.sub("첨부 이미지", "done" if not failed else "error", f"{len(got)}장" + (f" (실패 {len(failed)})" if failed else ""))
    c.sub("글 캡처", "running")
    hidden = "닉네임·회사명" if site == "blind" else "닉네임·IP"
    c.progress(0.7, f"글 화면을 캡처하는 중 ({hidden} 가림)")
    try:
        shot = c.deps.screenshot_post(client, post.url, c.dir / "post" / "screenshot.png", site=site)
        note = "수동 가림 확인 필요" if getattr(shot, "needs_manual_blur", False) else f"{hidden} 가림 완료"
        c.sub("글 캡처", "done", note + (" · 댓글 따로 캡처" if (getattr(shot, "parts", None) or {}).get("comments_file") else ""))
        c.job.artifact("screenshot", "post/screenshot.png")
        if getattr(shot, "parts", None):
            c.write_json("post/screenshot_parts.json", shot.parts)
    except Exception as e:  # noqa: BLE001  캡처는 있으면 좋은 것 — 실패해도 계속
        c.sub("글 캡처", "error", errors.screen_text(e))
        c.log(f"글 캡처 실패(계속 진행): {errors.screen_text(e)}")
    c.progress(1.0, "글 가져오기 끝")


# ------------------------------------------------------------------ 단계: read
READ_INSTRUCTION = """\
이 그림은 인터넷 커뮤니티 게시글에 첨부된 이미지다(세로로 긴 그림이면 위에서 아래 순서로 자른 조각들이다).
그림 속 글자를 한 글자도 바꾸지 말고 위에서 아래, 왼쪽에서 오른쪽 순서로 옮겨 적어라. 보이지 않는 글자를 추측해 만들지 말 것.
계정 아이디·닉네임·전화번호 같은 개인정보는 옮겨 적지 말고 [아이디] 처럼 표시한다.
JSON 객체 하나만 출력:
{"kind": "그림 종류를 짧게(예: 비교표, SNS 글 캡처, 글 카드, 사진, 짤)",
 "fan_made": 팬이 직접 만든 표·합성처럼 공식·확인된 자료가 아니면 true,
 "text": "옮겨 적은 글자(줄바꿈은 \\n)",
 "summary": "그림 내용 한 줄",
 "has_face": 사람 얼굴이 또렷이 보이면 true,
 "has_id_text": 아이디·닉네임·전화번호 같은 개인정보 글자가 보이면 true}"""


READ_COMMENTS_INSTRUCTION = """\
이 그림은 {site} 글에 달린 댓글 목록 캡처다(닉네임은 이미 흐리게 가려져 있다. 세로로 긴 그림이면 위에서 아래 순서의 조각들이다).
댓글 본문만 위에서 아래 순서로 한 댓글에 한 줄씩, 글자를 바꾸지 말고 옮겨 적어라.
다음은 옮기지 말고 건너뛴다: 욕설·비하·혐오 표현이 들어 있는 댓글, 특정 지역·성별·정치 성향을 깎아내리는 댓글,
글쓴이(환자·당사자)의 생사·외모를 두고 놀리는 댓글, 점(.)이나 한두 글자뿐인 댓글, 그림·짤만 있는 댓글, 광고.
닉네임·IP·@멘션·작성 시각·회사명은 적지 않는다.
JSON 객체 하나만 출력: {{"comments": ["댓글 본문", ...]}}"""


def _split_for_reading(path: Path) -> list:
    """세로로 긴 그림은 읽기 좋게 조각으로 (모델 해상도 한계)."""
    from PIL import Image
    from recap_shorts.images.captures import split_points
    with Image.open(path) as im:
        im = im.convert("RGB")
        if im.height <= 2.2 * im.width:
            return [path]
        n = min(5, max(2, round(im.height / (1.6 * im.width))))
        pts = split_points(im, n)
        return [im.crop((0, a, im.width, b)) for a, b in zip(pts[:-1], pts[1:])]


def step_read(c: Ctx) -> None:
    src = c.job.state["source"]
    site = site_of(src)
    post = c.read_json(f"post/post_{src['no']}.json")
    files = c.read_json("post/attachments.json")["files"]
    gname = post_label(src)
    if post.get("origin_gallery"):          # 실베 글: 원본 갤러리도 알려 준다(원문 끝의 '출처' 줄은 수집 때 지움)
        gname += f"(원본: {post['origin_gallery']} 갤러리)"
    where = "블라인드" if site == "blind" else "디시인사이드"
    head = (f"[게시글 1] {gname} 게시글, {(post.get('written_at') or '')[:16]} 작성\n"
            f"제목: {clean_text(post.get('title'))}\n본문: {clean_text(post.get('text')).replace(chr(10), ' ')}")
    parts, atts = [head], []
    # 키가 없거나 틀려서 못 읽었으면 같은 실패를 그림마다 되풀이하지 않고 첫 실패에서 멈춘다.
    # 그 안내를 단계 오류로 그대로 올려 “글자를 못 읽음”보다 원인이 먼저 보이게 한다.
    key_fail: BaseException | None = None
    for k, f in enumerate(files, 1):
        c.progress((k - 1) / max(1, len(files)), f"첨부 이미지 {k}/{len(files)}의 글자를 읽는 중")
        label = f"그림 {k}"
        c.sub(label, "running")
        try:
            r = c.deps.read_image(_split_for_reading(c.dir / f), READ_INSTRUCTION)
        except Exception as e:  # noqa: BLE001
            c.sub(label, "error", errors.screen_text(e))
            c.log(f"{label} 읽기 실패: {errors.screen_text(e)}")
            if errors.kind_of(e) in (errors.NO_KEY, errors.BAD_KEY):
                key_fail = e
                break
            atts.append({"n": k, "file": f, "kind": "읽기 실패", "fan_made": False, "text": ""})
            continue
        kind = str(r.get("kind") or "그림").strip()
        fan = bool(r.get("fan_made"))
        tag = f"팬이 만든 {kind}(확인 안 된 자료)" if fan else kind
        text = clean_text(str(r.get("text") or ""))
        parts.append(f"\n[게시글 1 첨부 그림 {k}: {tag}]\n{text}".rstrip())
        atts.append({"n": k, "file": f, "kind": kind, "fan_made": fan, "summary": str(r.get("summary") or ""),
                     "has_face": bool(r.get("has_face")), "has_id_text": bool(r.get("has_id_text")), "text": text})
        c.sub(label, "done", f"{kind}{' · 팬 제작' if fan else ''}{' · 얼굴' if r.get('has_face') else ''}"
                              f"{' · 아이디' if r.get('has_id_text') else ''}")
    if key_fail is not None:
        raise key_fail
    if files and not any(a.get("text") for a in atts):
        raise RuntimeError("첨부 그림의 글자를 하나도 읽지 못했습니다")
    cm = c.dir / "post" / "comments.png"
    if cm.is_file():                         # 댓글 캡처(닉네임 가림) — 반응 문장·캡처 화면 재료
        c.sub(f"{where} 댓글", "running")
        try:
            r = c.deps.read_image(_split_for_reading(cm), READ_COMMENTS_INSTRUCTION.format(site=where))
            lines = [clean_text(re.sub(r"\s*-\s*dc App\s*$", "", str(x))) for x in (r.get("comments") or []) if str(x).strip()]
            lines = [x for x in lines if len(x) >= 4][:30]
            if lines:
                parts.append(f"\n[게시글 1 {where} 댓글 캡처(닉네임 가림, 욕설·비하 댓글 뺌)]\n" + "\n".join(f"- {x}" for x in lines))
                atts.append({"n": len(files) + 1, "file": "post/comments.png", "kind": f"{where} 댓글 캡처", "dc_comments": True,
                             "fan_made": False, "summary": f"이 글에 달린 {where} 댓글", "has_face": False, "has_id_text": True,
                             "text": "\n".join(lines)})
            c.sub(f"{where} 댓글", "done", f"{len(lines)}개 옮김")
        except Exception as e:  # noqa: BLE001  댓글은 있으면 좋은 것
            c.sub(f"{where} 댓글", "error", errors.screen_text(e))
            c.log(f"{where} 댓글 읽기 실패(계속 진행): {errors.screen_text(e)}")
    caution = ("첨부 그림이 확인 안 된 팬 제작물이면 그 안의 이름·숫자는 단정하지 말고 전해 들은 말로 쓴다. "
               "특정인 비난·성적 대상화·확인 안 된 의혹은 다루지 않는다.")
    topic = {"topic": f"{gname} 게시글 「{clean_text(post.get('title'))}」의 내용 중 가장 흥미로운 반전 하나를 잡아 이야기한다. {caution}",
             "source_kind": "community", "source_url": src["url"],
             "tone_end": "웃기면서도 씁쓸한 감탄이나 물음 한 줄",
             "source_text": "\n".join(parts), "attachments": atts}
    c.write_json("topic.json", topic)
    c.job.artifact("topic", "topic.json")
    c.progress(1.0, f"자료 읽기 끝 — 글자 {len(topic['source_text'])}자")


# ------------------------------------------------------------------ 단계: script
def _copy_script_files(job: Job) -> None:
    dst = job.dir / "script_final"
    dst.mkdir(parents=True, exist_ok=True)
    for name in ("script.json", "script_check.json", "script_draft.json", "script_draft_check.json"):
        if (job.dir / "script" / name).is_file():
            shutil.copy(job.dir / "script" / name, dst / name)


def step_script(c: Ctx) -> None:
    from recap_shorts.script.pipeline import generate_script
    topic = c.read_json("topic.json")
    llm = c.deps.script_llm(c.opt["script_llm"], c.opt["script_model"])
    model = getattr(llm, "model", "?")
    c.progress(0.05, f"대본 초안을 쓰는 중 ({model})")
    c.sub("초안", "running")
    c.sub("다듬기", "todo")
    c.sub("사실 가드", "todo")

    def on_stage(name, status, note):
        label = {"draft": "초안", "refine": "다듬기"}[name]
        st = {"start": "running", "done": "done", "error": "error"}[status]
        c.sub(label, st, note)
        if name == "draft" and status == "done":
            c.progress(0.5, "초안 끝 — 소리 내어 읽기 기준으로 다듬는 중")
        elif name == "refine" and status != "start":
            c.progress(0.9, "사실 가드 확인 중")

    script, check = generate_script(topic, c.dir / "script", llm, refine=bool(c.opt["refine"]), on_stage=on_stage)
    _copy_script_files(c.job)
    ref = check.get("refine") or {}
    c.sub("사실 가드", "done" if check["ok"] else "error",
          f"삭제 {len(check['deleted'])}문장 · flags {len(check['flags'])} · 음절 {check['checks']['syllables']['total']}")
    if ref and not ref.get("applied"):
        c.log(f"다듬기 미적용: {errors.screen_text(ref.get('error'), '이유를 알 수 없음')}")
    c.job.artifact("script", "script_final/script.json")
    c.job.artifact("script_draft", "script_final/script_draft.json")
    c.job.state["title"] = " ".join(script.get("title_lines") or []) or c.job.state["title"]
    c.progress(1.0, f"대본 끝 — 문장 {len(script['sentences']) + 2}개")


# ------------------------------------------------------------------ 단계: voice
def step_voice(c: Ctx) -> None:
    script = c.read_json("script_final/script.json")
    from recap_shorts import modelfiles, settings
    engine = c.opt["tts_engine"] or settings.voice_engine()
    if engine == "supertonic" and not modelfiles.ok("supertonic"):
        raise RuntimeError("로컬 Supertonic 모델 경로를 확인하세요. 기존 모델 파일을 사용해야 합니다")
    c.progress(0.1, f"목소리 합성 중 ({engine}, 로컬)")
    c.sub("합성", "running")
    c.sub("받아쓰기 확인", "todo")
    t0 = time.time()
    tm = c.deps.make_voice(script, c.dir / "voice", engine, bool(c.opt["check_asr"]))
    c.sub("합성", "done", f"{time.time() - t0:.0f}초 · {tm.get('duration', 0):.1f}초 분량")
    mean = tm.get("asr_mean_match")
    clipped = tm.get("clipped_ends") or []
    c.sub("받아쓰기 확인", "done", (f"일치 평균 {mean}" if mean is not None else "건너뜀")
          + (f" · 말끝 잘림 의심 {len(clipped)}곳" if clipped else ""))
    for w in tm.get("warnings") or []:
        if "말 중간에서 잘렸을" in w:
            c.log(f"목소리: {w}")
    c.job.artifact("voice", "voice/voice.wav")
    c.job.artifact("timings", "voice/timings.json")
    c.progress(1.0, "목소리 끝")


# ------------------------------------------------------------------ 단계: assets
def sentence_infos(script: dict, timings: dict) -> list[dict]:
    """타이밍 index(0=훅, 1..=본문, 마지막=끝 줄)마다 {index, role, text, hint}."""
    body = script.get("sentences") or []
    out = []
    for s in timings["sentences"]:
        i, role = s["index"], s.get("role", "body")
        hint = ""
        if role == "hook":
            hint = "영상 첫 화면: 주제 전체를 한눈에 보여 주는 분위기 있는 장면 — " + " ".join(script.get("title_lines") or [])
        elif role == "body" and 0 <= i - 1 < len(body):
            hint = body[i - 1].get("image_hint") or ""
        out.append({"index": i, "role": role, "text": s["text"], "hint": hint})
    return out


def wants_ai(info: dict) -> bool:
    """문장마다 AI 그림 1장(측정에서 장면의 26%가 AI 그림, 사진·캡처와 섞음). 캡처 힌트가 있어도 만든다 —
    힌트의 대부분이 '캡처'로 적히면 적은 그림 수로 많은 샷을 돌리게 되어 막힌다.
    실존 인물이 나와야 하는 장면만 만들지 않는다."""
    h = info["hint"]
    return bool(h) and "실제 사진 필요" not in h


def capture_wants(script: dict, timings: dict) -> list[tuple[int, str]]:
    """문장 image_hint 의 "캡처: …" 부분 → [(타이밍 index, "찾을 부분 — 문장: …")]. 한 힌트에 캡처가 여러 개면 각각."""
    out = []
    for info in sentence_infos(script, timings):
        for seg in re.split(r"\s*/\s*", info["hint"] or ""):
            if seg.strip().startswith("캡처:"):
                what = seg.split(":", 1)[1].strip()
                if what:
                    out.append((info["index"], f"{what} — 문장: \"{info['text']}\""))
    return out


QUALITY_FIX = {   # 품질 검사에서 걸린 결함 → 다시 만들 때 프롬프트에 덧붙일 말
    "sunset": "Use neutral daytime or indoor lighting, no sunset glow.",
    "filler": "Keep the frame on the main subject only, no decorative props.",
    "blank_screen": "Do not make a glowing blank screen the subject; show the physical thing or place instead.",
    "neon_float": "No neon light and no floating graphics; an ordinary real scene.",
    "fake_text": "Angle or defocus any paper, sign or screen so that no letters or logos are visible.",
    "artifacts": "Keep hands and objects simple and anatomically correct, or leave hands out.",
    "subject_missing": "Make the main subject large, clear and centred.",
    "western": "Clearly a South Korean place.",
}


QUALITY_KO = {   # 결함 이름(검수 화면에 보이는 이유 글)
    "sunset": "노을빛", "filler": "채움 소품", "blank_screen": "빈 화면", "neon_float": "네온·뜬 그래픽",
    "fake_text": "가짜 글자", "artifacts": "뭉개진 AI 티", "subject_missing": "주인공 안 보임", "western": "서양식 배경",
}


def _quality_pass(c: Ctx, entries: list[dict]) -> None:
    """AI 이미지 결함 검사(Codex 비전 1회) → 나쁜 장을 한 번씩 다시 만든다(많아야 image_redo_max 장).
    AI 이미지 퀄리티를 높이려고 스톡 사진 티(노을·빈 화면·네온)를 걸러 낸다."""
    from recap_shorts.images import provider, quality
    ai_ok = [e for e in entries if e["kind"] == "ai" and e["status"] == "ok"]
    if not ai_ok:
        return
    c.sub("이미지 품질", "running", f"{len(ai_ok)}장 검사")
    rv = c.deps.image_quality([c.dir / e["file"] for e in ai_ok], [e.get("hint") or "" for e in ai_ok])
    rows = rv.get("items") or []
    if not rows:
        c.sub("이미지 품질", "done", "검사 못 함 — 사람 확인")
        return
    for e, row in zip(ai_ok, rows):
        e["quality"] = row
    redo = quality.pick_redo(rv, int(c.opt.get("image_redo_max", 3)))
    if any(e.get("engine") == "codex-imagegen" for e in ai_ok):
        # Native imagegen belongs to the supervising Codex session. Keep the
        # verified originals and expose precise feedback for its visual review;
        # a background Python retry must not invoke a paid image API instead.
        for k in redo:
            _flag_left(ai_ok[k], rows[k], "Codex에서 확인 후 필요하면 native imagegen으로 다시 생성")
        c.sub("이미지 품질", "done", f"확인할 이미지 {len(redo)}장" if redo else "결함 없음")
        return
    fixed, redone = 0, []
    for k in redo:
        e, row = ai_ok[k], rows[k]
        fixes = [QUALITY_FIX[f] for f in quality.FLAGS if row.get(f) and f in QUALITY_FIX]
        if row.get("similar_to"):
            fixes.append("Use a clearly different subject and camera distance from the other shots.")
        en = (e.get("prompt_en") or "").rstrip(". ") + ". " + " ".join(fixes)
        full = provider.build_prompt(en)
        out = c.dir / e["file"]
        tmp = out.with_name(out.stem + ".redo.png")
        r = c.deps.generate_image(full, tmp)
        if r.get("ok") and tmp.is_file():
            shutil.move(str(tmp), str(out))
            e.update(prompt=full, prompt_en=en, regenerated_for=[f for f in quality.FLAGS if row.get(f)] or ["similar"],
                     seconds=r.get("seconds"))
            fixed += 1
            redone.append(k)
        else:
            c.log(f"AI 이미지 다시 만들기 실패(문장 {e['sentence']}): {errors.screen_text(r.get('error'), '이미지를 만들지 못했습니다')}")
            _flag_left(e, row, "다시 만들기 실패 — 결함 남음")
    left = _recheck(c, [ai_ok[k] for k in redone])
    note = f"결함 {len(redo)}장 → 다시 만듦 {fixed}장" + (f" · 그래도 남음 {left}장" if left else "") if redo else "결함 없음"
    c.sub("이미지 품질", "done", note)
    c.log(f"이미지 품질 검사: {len(ai_ok)}장 중 다시 만들 장 {[ai_ok[k]['sentence'] for k in redo]}, 성공 {fixed}, 그래도 남음 {left}")


def _quality_flags(row: dict) -> list[str]:
    from recap_shorts.images.quality import FLAGS
    return [f for f in FLAGS if row.get(f)]


def _flag_left(e: dict, row: dict, why: str) -> None:
    """결함이 남은 AI 그림 → 다시 만들기 추천 + 이유(검수 화면·장면 편집기에 보인다)."""
    flags = [QUALITY_KO.get(f, f) for f in _quality_flags(row)]
    e["recommend_regenerate"] = True
    e["review_reason"] = why + (f"({', '.join(flags)})" if flags else "") + (f": {row['reason']}" if row.get("reason") else "")


def _recheck(c: Ctx, redone: list[dict]) -> int:
    """다시 만든 장만 한 번 더 검사(Codex 비전 1회). 다시 만든 뒤에도 빈 격자 표가 그대로 남는 경우가 있다.
    심한 결함이 하나라도, 가벼운 결함이 2개 이상 남으면 표시한다 — 다시 검사한 장끼리라 '겹침'·'가장 나쁜 장'은 보지 않는다.
    검사가 실패하면 다시 만든 그림을 아무도 안 본 것이므로 같이 표시한다. 표시한 장 수를 돌려준다."""
    from recap_shorts.images.quality import SEVERE
    if not redone:
        return 0
    rv = c.deps.image_quality([c.dir / e["file"] for e in redone], [e.get("hint") or "" for e in redone])
    rows = rv.get("items") or []
    if not rows:
        for e in redone:
            _flag_left(e, e.get("quality") or {}, "다시 만든 뒤 검사 못 함 — 처음 결함")
        return len(redone)
    left = 0
    for e, row in zip(redone, rows):
        e["quality_before"], e["quality"] = e.get("quality"), row
        flags = _quality_flags(row)
        if any(f in SEVERE for f in flags) or len(flags) >= 2:
            _flag_left(e, row, "다시 만들어도 결함 남음")
            left += 1
    return left


def _panel_cards(c: Ctx, a: dict, src: Path, cap_dir: Path, entries: list, make_card, wants) -> bool:
    """칸 쌓기 첨부 → 고른 칸마다 카드. 칸 쌓기가 아니거나 고른 칸이 없으면 False(기존 방식으로 간다)."""
    from PIL import Image
    from recap_shorts.images.captures import panel_boxes
    boxes = panel_boxes(src)
    if not boxes:
        return False
    sents = [i for i in sentence_infos(c.read_json("script_final/script.json"), c.read_json("voice/timings.json"))
             if i["role"] != "hook"]
    text_of = {i["index"]: i["text"] for i in sents}
    res = c.deps.pick_panels(src, boxes, sents)
    picks = [p for p in (res or {}).get("picks") or [] if p.get("panels")]
    if not picks:
        return False
    faces, ids = res.get("faces") or {}, res.get("ids") or {}
    pdir = c.dir / "post" / "panels"
    pdir.mkdir(parents=True, exist_ok=True)
    made: list[dict] = []
    with Image.open(src) as im:
        im = im.convert("RGB")
        for p in picks:
            sent = p["sentence"]
            text = text_of.get(sent, "")
            for j, k in enumerate(p["panels"]):
                box = boxes[k]
                png = pdir / f"a{a['n']:02d}_{k:02d}.png"
                if not png.is_file():
                    im.crop(box).save(png)
                try:
                    f = c.deps.locate_regions(png, [text])
                    private = [tuple(b) for b in ((f or {}).get("private") or [])]
                except Exception as e:  # noqa: BLE001
                    c.log(f"캡처 {a['n']} 칸 {k} 가릴 곳 찾기 실패: {errors.reason(e)}")
                    private = []
                name = f"c{a['n']:02d}_s{sent:02d}" + (f"_{j + 1}" if j else "")
                out = cap_dir / f"{name}.png"
                make_card(png, out, blur_boxes=private, label=None)
                fid = bool(faces.get(k) or ids.get(k) or faces.get(str(k)) or ids.get(str(k)))
                if private:
                    reason = f"자동 가림 {len(private)}곳 — 빠진 곳은 '가리기'로 더하기"
                else:
                    reason = "얼굴·아이디가 보일 수 있음 — 가림 확인" if fid else None
                made.append({"kind": "cap", "status": "ok", "file": rel(c.job, out), "src": rel(c.job, png),
                             "attachment": a["n"], "inspect": None, "sentence": sent, "role": "focus", "want": text,
                             # crop 은 src(잘라 낸 칸 그림) 기준이라 None — 원본 속 칸 위치는 panel_box 에.
                             # 원본 좌표를 crop 에 넣으면 '가리기 적용'이 칸 그림 밖을 잘라 검은 카드가 된다
                             "crop": None, "panel_box": list(box), "panel": k, "blur_boxes": [list(b) for b in private],
                             "blurred": bool(private), "highlight": None, "needs_review": fid,
                             "review_reason": reason, "unblurred_risk": bool(fid and not private)})
    entries.extend(made)
    c.log(f"캡처 {a['n']}: 장면 {len(boxes)}칸 중 {len(made)}칸 골라 씀")
    return True


def _sensitive_pass(c: Ctx, entries: list[dict]) -> None:
    """자극적인 그림에 흑백/흐림을 자동으로 입힌다(images/sensitive.py, config/sensitive.json).
    잔인한 그림을 흑백·흐림으로 처리해 광고 제한(노란 딱지)을 받을 가능성을 줄인다.
    작업 옵션 sensitive_treatment=False 면 아무것도 하지 않는다(호출 0). 판정이 실패하면 처리 없이 지나가고 소항목에 이유를 남긴다."""
    from recap_shorts.images import sensitive
    cfg = sensitive.load_config()
    if not c.opt.get("sensitive_treatment", True) or not cfg["enabled"]:
        return
    c.sub("민감한 그림", "running")
    try:
        res = sensitive.run(c.dir, entries, read_fn=c.deps.sensitive_read, cfg=cfg, log=c.log)
    except Exception as e:  # noqa: BLE001
        c.log(f"민감한 그림 검사 실패(처리 없이 계속): {errors.screen_text(e)}")
        c.sub("민감한 그림", "error", f"건너뜀 — {errors.screen_text(e)}")
        return
    t = res["treated"]
    parts = [f"{res['checked']}장 검사" + (f" · 흑백 {t['gray']}장" if t["gray"] else "") +
             (f" · 흑백+흐림 {t['gray_blur']}장" if t["gray_blur"] else "") +
             (f" · Codex {res['calls']}회" if res["calls"] else "")]
    if res["errors"]:
        parts.append(f"검사 못 함 {len(res['errors'])}장: {errors.reason(res['errors'][0])}")
        c.log(f"민감한 그림 검사 못 함 {len(res['errors'])}장(처리 없이 둠): {errors.reason(res['errors'][0])}")
    c.sub("민감한 그림", "error" if res["errors"] else "done", " · ".join(parts))
    if t["gray"] or t["gray_blur"]:
        c.log(f"민감한 그림 {t['gray'] + t['gray_blur']}장에 흑백/흐림 처리(약함 {res['levels']['mild']}·강함 {res['levels']['strong']})")


BODY_MIN_H = 40   # 본문 상자가 이보다 낮으면(글자가 거의 없음) 본문 캡처를 쓰지 않는다
SAME_BOX = 0.85   # 한 그림에서 두 문장이 잘라 갈 곳이 이만큼(IoU) 겹치면 같은 곳으로 보고 앞 문장만 카드를 만든다


def post_body_attachment(job_dir: Path, atts: list[dict]) -> dict | None:
    """그림 없이 글만 있는 글이면 본문 캡처를 첨부 0번처럼 쓴다 — 대본의 '캡처: 본문 …' 힌트가 잘라 갈 곳.

    본문 상자: 블라인드는 캡처 때 적은 parts["body"], 디시는 글 머리 아래 ~ 추천 상자 위.
    첨부 그림이 있는 글은 본문 캡처 속 그림이 첨부와 겹치므로 쓰지 않는다(댓글 캡처만 있으면 글만 있는 글로 본다).
    잘라 낸 그림은 post/body.png(캡처 때 이미 닉네임 흐림). 상자를 못 찾으면 None."""
    if any(not a.get("dc_comments") for a in atts):
        return None
    shot, parts_p = job_dir / "post" / "screenshot.png", job_dir / "post" / "screenshot_parts.json"
    if not (shot.is_file() and parts_p.is_file()):
        return None
    parts = json.loads(parts_p.read_text(encoding="utf-8"))
    box = parts.get("body")
    if not box and parts.get("head") and parts.get("recommend"):
        head, rec = parts["head"], parts["recommend"]
        box = [head[0], head[3], head[2], rec[1]]
    if not box:
        return None
    from PIL import Image
    with Image.open(shot) as im:
        x0, y0 = max(0, int(box[0])), max(0, int(box[1]))
        x1, y1 = min(im.width, int(box[2])), min(im.height, int(box[3]))
        if x1 - x0 < BODY_MIN_H or y1 - y0 < BODY_MIN_H:
            return None
        im.crop((x0, y0, x1, y1)).save(job_dir / "post" / "body.png")
    return {"n": 0, "file": "post/body.png", "kind": "글 본문 캡처", "summary": "글 본문", "body": True}


def _image_input_hash(info: dict) -> str:
    """A changed scene or prompt template invalidates its cached generation."""
    from recap_shorts.images import provider
    from recap_shorts.images.codex_imagegen import digest
    values = [info["index"], info.get("role"), info["text"], info["hint"],
              provider.HINT_INSTRUCTION, provider.COMMON_PREFIX, provider.COMMON_SUFFIX]
    return digest(json.dumps(values, ensure_ascii=False))


def _image_prompt_cache(c: Ctx) -> dict:
    path = c.dir / "assets" / "image_prompts.json"
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("prompts"), dict):
        raise ValueError("assets/image_prompts.json 형식이 올바르지 않습니다")
    return data["prompts"]


def _cached_ai(entry: dict, info: dict, cache: dict, job_dir: Path) -> bool:
    """Reuse only a decodable file for this exact scene and prompt."""
    from PIL import Image
    from recap_shorts.images import codex_imagegen, provider
    key = info["input_sha256"]
    if entry.get("input_sha256") != key or entry.get("status") not in ("ok", "reused"):
        return False
    en = cache.get(key)
    if not isinstance(en, str) or entry.get("prompt") != provider.build_prompt(en):
        return False
    path = job_dir / entry["file"]
    if entry.get("engine") == codex_imagegen.PROVIDER:
        return codex_imagegen.current(entry["prompt"], path)
    try:
        with Image.open(path) as im:
            im.verify()
        with Image.open(path) as im:
            im.load()
        return True
    except (OSError, ValueError, SyntaxError):
        return False


def _prompts_for(c: Ctx, infos: list[dict], cache: dict) -> list[str]:
    """Translate each scene once; retries use the same native-imagegen prompt."""
    missing = [i for i in infos if i["input_sha256"] not in cache]
    if missing:
        values = c.deps.hint_prompts([f"(내레이션: {i['text']}) {i['hint']}" for i in missing])
        if len(values) != len(missing) or any(not isinstance(v, str) or not v.strip() for v in values):
            raise ValueError("이미지 프롬프트 개수 또는 내용이 올바르지 않습니다")
        cache.update({i["input_sha256"]: value.strip() for i, value in zip(missing, values)})
        c.write_json("assets/image_prompts.json", {"version": 1, "prompts": cache})
    return [cache[i["input_sha256"]] for i in infos]


def step_assets(c: Ctx) -> None:
    from recap_shorts.images import provider, regions
    from recap_shorts.images.captures import make_cards_for_image
    from recap_shorts.render.cards import make_card
    script, timings = c.read_json("script_final/script.json"), c.read_json("voice/timings.json")
    topic = c.read_json("topic.json")
    entries: list[dict] = []
    # Cache source-derived cards and their Codex analysis while native imagegen
    # is pending. Changed source bytes, text, script or options invalidate it.
    from recap_shorts.images.codex_imagegen import digest, file_digest
    source_files = [a.get("file") for a in topic.get("attachments", []) if a.get("file")]
    source_files += ["post/screenshot.png", "post/screenshot_parts.json", "post/comments.png"]
    fingerprints = {f: file_digest(c.dir / f) for f in source_files}
    capture_key = digest(json.dumps([script, timings, topic, fingerprints, c.opt.get("entity_images", True)],
                                   ensure_ascii=False, sort_keys=True))
    capture_path = c.dir / "assets" / "prepared_captures.json"
    prepared = json.loads(capture_path.read_text(encoding="utf-8")) if capture_path.is_file() else {}
    cached = prepared.get("entries")
    if (prepared.get("input_sha256") == capture_key and isinstance(cached, list)
            and all((c.dir / e["file"]).is_file() for e in cached)):
        entries = cached
        c.sub("캡처 카드", "done", f"이미 준비한 {len(entries)}장 사용")
    else:
        # 1) 캡처 카드 — (가) 문장이 말하는 부분만 잘라 낸 확대 카드 (나) 첨부 그림 통째 (다) 글 머리(제목·조회 줄)
        #    가릴 곳(아이디·프로필·얼굴·번호·욕설 낱말)은 비전 모델이 같은 호출에서 찾아 모자이크한다.
        c.sub("캡처 카드", "running")
        atts = [a for a in topic.get("attachments", []) if a.get("file") and (c.dir / a["file"]).is_file()]
        try:
            body = post_body_attachment(c.dir, atts)
        except Exception as e:  # noqa: BLE001
            body = None
            c.log(f"본문 캡처 자르기 실패(건너뜀): {errors.reason(e)}")
        if body:
            atts.insert(0, body)
        wants = capture_wants(script, timings)
        cap_dir = c.dir / "assets" / "cap"
        cap_dir.mkdir(parents=True, exist_ok=True)
        for n, a in enumerate(atts):
            c.progress(0.03 + 0.22 * n / max(1, len(atts)), f"캡처 {n + 1}/{len(atts)}: 문장이 말하는 부분과 가릴 곳을 찾는 중")
            src = c.dir / a["file"]
            # 칸 쌓기 그림(장면 수십 장을 회색 줄로 이은 것)은 통째·조각내기 대신 주제에 맞는 칸만 골라 쓴다
            if not a.get("dc_comments") and not a.get("body"):
                try:
                    if _panel_cards(c, a, src, cap_dir, entries, make_card, wants):
                        continue
                except Exception as e:  # noqa: BLE001
                    c.log(f"캡처 {a['n']} 칸 고르기 실패(기존 방식으로): {errors.reason(e)}")
            found = None
            try:
                found = c.deps.locate_regions(src, [t for _, t in wants])
            except Exception as e:  # noqa: BLE001
                c.log(f"캡처 {a['n']} 위치 찾기 실패(통째 카드만): {errors.reason(e)}")
            private = [tuple(b) for b in ((found or {}).get("private") or [])]
            if private:
                what = list((found or {}).get("private_what") or []) + [""] * len(private)
                own = bool(a.get("dc_comments") or a.get("body"))       # 우리가 찍은 캡처: 이름은 캡처 때 이미 흐림
                try:                                  # 비전 상자가 글줄에서 빗나가도 닉네임 줄을 끝까지 덮게
                    snapped = regions.snap_private(src, private)
                    private = [regions.keep_off_text(src, b) if own and w == "아이디" else s
                               for b, s, w in zip(private, snapped, what)]
                except Exception:  # noqa: BLE001
                    pass
            info = None
            try:
                info = c.deps.clef_inspect(src, "capture")
            except Exception:  # noqa: BLE001
                info = None
            risky = bool(info and ((info.get("face") or 0) >= 0.5 or (info.get("personal_text") or 0) >= 0.5))
            flagged = bool(a.get("has_face") or a.get("has_id_text"))
            if private:
                reason, need = f"자동 가림 {len(private)}곳 — 빠진 곳은 '가리기'로 더하기", False
            elif risky or flagged:
                reason, need = "얼굴·아이디가 보일 수 있음 — 가림 확인", True
            elif info is None or info.get("face") is None or info.get("personal_text") is None:
                reason, need = "자동 점검 없음 — 사람 확인", True
            else:
                reason, need = None, False
            base = {"kind": "cap", "status": "ok", "src": a["file"], "attachment": a["n"], "inspect": info,
                    "blur_boxes": [list(b) for b in private], "blurred": bool(private),
                    "needs_review": need, "review_reason": reason,
                    "unblurred_risk": bool((risky or flagged) and not private)}
            try:
                seen: dict[int, int] = {}
                taken: list = []                      # 이 그림에서 이미 잘라 간 곳 — 거의 같은 곳이면 같은 카드가 두 번 나온다
                for (sent, text), box in zip(wants, (found or {}).get("parts") or []):
                    if not box:
                        continue
                    if any(regions.overlap_ratio(box, t) >= SAME_BOX for t in taken):
                        c.log(f"캡처 {a['n']}: 문장 {sent} 이 앞 문장과 같은 곳이라 카드를 더 만들지 않음")
                        continue
                    taken.append(box)
                    seen[sent] = seen.get(sent, 0) + 1
                    name = f"c{a['n']:02d}_s{sent:02d}" + (f"_{seen[sent]}" if seen[sent] > 1 else "")
                    out = cap_dir / f"{name}.png"
                    strip = None
                    if a.get("dc_comments") or a.get("body"):   # 댓글·글 본문 줄: 글자만 띠로 잘라(길면 접어) 크게 — PC 폭 캡처는 글자가 작다
                        strip = regions.text_strip(src, box, private, c.dir / "post" / "strips" / f"{name}.png")
                    if strip:
                        make_card(strip, out, label=None, max_upscale=regions.FOCUS_UPSCALE,
                                  backdrop=regions.context_of(src, box, private))
                        entries.append({**base, "file": rel(c.job, out), "src": rel(c.job, strip), "sentence": sent,
                                        "role": "focus", "want": text, "crop": None, "found": list(box),
                                        "blur_boxes": [], "highlight": None})
                        continue
                    crop, hl = regions.focus_crop(src, box)       # 자르는 선·빨간 테두리를 글줄 사이로
                    make_card(src, out, crop=crop, blur_boxes=private, label=None, highlight=hl,
                              max_upscale=regions.FOCUS_UPSCALE)
                    entries.append({**base, "file": rel(c.job, out), "sentence": sent, "role": "focus", "want": text,
                                    "crop": list(crop), "found": list(box), "highlight": list(hl) if hl else None})
                if not a.get("dc_comments"):          # 댓글 목록은 통째로는 안 씀(잘라 낸 댓글만)
                    for card in make_cards_for_image(src, cap_dir, f"c{a['n']:02d}", blur_boxes=private):
                        entries.append({**base, "file": rel(c.job, card["file"]), "sentence": None, "crop": card.get("crop")})
            except Exception as e:  # noqa: BLE001
                c.log(f"캡처 카드 {a['n']} 실패: {errors.reason(e)}")
        parts_p = c.dir / "post" / "screenshot_parts.json"
        if parts_p.is_file() and (c.dir / "post" / "screenshot.png").is_file():
            head = json.loads(parts_p.read_text(encoding="utf-8")).get("head")
            where = "블라인드" if site_of(c.job.state["source"]) == "blind" else "디시"
            hidden = "회사명" if where == "블라인드" else "닉네임"
            try:
                img = regions.dc_head_image(c.dir / "post" / "screenshot.png", head, c.dir / "post" / "dc_head.png") if head else None
                if img:
                    out = cap_dir / "dc_head.png"
                    make_card(img, out, label=None, max_upscale=regions.FOCUS_UPSCALE)
                    entries.append({"file": rel(c.job, out), "kind": "cap", "status": "ok", "src": "post/dc_head.png",
                                    "attachment": None, "sentence": None, "role": "dc_head", "crop": None, "blur_boxes": [], "blurred": False,
                                    "needs_review": False, "review_reason": f"{where} 글 머리({hidden}은 캡처 때 흐림)"})
            except Exception as e:  # noqa: BLE001
                c.log(f"{where} 글 머리 카드 실패: {errors.reason(e)}")
        nf = sum(1 for e in entries if e.get("role") == "focus")
        c.sub("캡처 카드", "done", f"{len(entries)}장 (문장 부분 {nf}장)")
        # 1-2) 대상 사진 — 대본이 말하는 캐릭터·작품 등의 실제 사진(위키백과·공용). 실패해도 작업은 계속한다.
        if c.opt.get("entity_images", True):
            c.sub("대상 사진", "running")
            try:
                res = c.deps.entity_images(c.dir, script, timings, topic)
                entries += res.get("entries", [])
                parts = []
                if res.get("found"):
                    parts.append("찾음 " + ", ".join(res["found"]))
                if res.get("missed"):
                    parts.append("못 찾음 " + ", ".join(res["missed"]))
                c.sub("대상 사진", "done", " · ".join(parts) + (f" · Codex {res.get('calls', 0)}회" if res.get("calls") else "")
                      if parts else "사진이 필요한 대상 없음")
                for line in res.get("log", []):
                    c.log(f"대상 사진: {line}")
            except Exception as e:  # noqa: BLE001
                c.log(f"대상 사진 찾기 실패(건너뜀): {errors.screen_text(e)}")
                c.sub("대상 사진", "error", f"건너뜀 — {errors.screen_text(e)}")
        c.write_json("assets/prepared_captures.json", {"input_sha256": capture_key, "entries": entries})
    # 2) Native imagegen: queue missing files, then resume this incomplete stage.
    from recap_shorts.images import codex_imagegen
    infos = [i for i in sentence_infos(script, timings) if wants_ai(i)][:int(c.opt["max_ai_images"])]
    for info in infos:
        info["input_sha256"] = _image_input_hash(info)
    cache = _image_prompt_cache(c)
    prev_path = c.dir / "assets" / "assets.json"
    previous = json.loads(prev_path.read_text(encoding="utf-8")) if prev_path.is_file() else []
    prev = {e["sentence"]: e for e in previous if e.get("kind") == "ai"}
    reuse_dir = Path(c.opt["reuse_images_from"]).expanduser() if c.opt["reuse_images_from"] else None
    reuse_files = sorted(f for f in reuse_dir.glob("*.png") if not f.name.endswith(".orig.png")) if reuse_dir and reuse_dir.is_dir() else []
    gen_ok = bool(infos and not c.opt["no_image_gen"] and c.deps.image_available())
    limit = c.opt["image_gen_limit"]
    n_real = (len(infos) if limit is None else min(max(0, int(limit)), len(infos))) if gen_ok else 0
    real, copy_ = infos[:n_real], infos[n_real:]
    kept = [prev[i["index"]] for i in real
            if i["index"] in prev and _cached_ai(prev[i["index"]], i, cache, c.dir)]
    entries += kept
    kept_indices = {e["sentence"] for e in kept}
    todo = [i for i in real if i["index"] not in kept_indices]
    outputs = [c.dir / "assets" / "ai" / f"ai_{i['index']:02d}.png" for i in real]
    manifest = c.dir / "assets" / codex_imagegen.MANIFEST_NAME
    codex_imagegen.retain_outputs(manifest, outputs)
    pending = []
    if not infos:
        c.sub("AI 이미지", "done", "필요한 문장 없음")
    elif not gen_ok and not reuse_files:
        c.sub("AI 이미지", "done", "AI 생성 꺼짐 — 캡처 카드만 사용")
    else:
        c.sub("AI 이미지", "running", f"검증된 이미지 {len(kept)}장 · 요청/확인 {len(todo)}장")
        prompts = _prompts_for(c, todo, cache)
        (c.dir / "assets" / "ai").mkdir(parents=True, exist_ok=True)
        # Registration is cheap and sequential: one manifest update per image,
        # with no subprocess or network generation hidden inside this loop.
        for k, (info, en) in enumerate(zip(todo, prompts)):
            out = c.dir / "assets" / "ai" / f"ai_{info['index']:02d}.png"
            full = provider.build_prompt(en)
            result = c.deps.generate_image(full, out)
            ok = bool(result.get("ok") and out.is_file())
            state = "ok" if ok else ("awaiting_images" if result.get("pending") else "error")
            entry = {"file": rel(c.job, out), "kind": "ai", "sentence": info["index"],
                     "status": state, "error": result.get("error"), "hint": info["hint"],
                     "input_sha256": info["input_sha256"], "prompt": full, "prompt_en": en,
                     "engine": result.get("engine"), "seconds": result.get("seconds"),
                     "sha256": result.get("sha256")}
            entries.append(entry)
            if result.get("pending"):
                pending.append(entry)
            c.progress(0.35 + 0.5 * (k + 1) / max(1, len(todo)), f"AI 이미지 요청 확인 {k + 1}/{len(todo)}")
        for k, info in enumerate(copy_):
            if not reuse_files:
                break
            srcf = reuse_files[(n_real + k) % len(reuse_files)]
            out = c.dir / "assets" / "ai" / f"ai_{info['index']:02d}.png"
            if srcf.resolve() != out.resolve():
                shutil.copy(srcf, out)
            entries.append({"file": rel(c.job, out), "kind": "ai", "sentence": info["index"],
                            "status": "reused", "error": None, "hint": info["hint"],
                            "input_sha256": info["input_sha256"], "reused_from": str(srcf)})
        # Persist complete and pending assets before yielding. Captures do not
        # make a requested native image optional, and resume never reruns TTS.
        c.write_json("assets/assets.json", entries)
        c.job.artifact("assets", "assets/assets.json")
        if pending:
            c.sub("AI 이미지", "awaiting_images", f"Codex 내장 imagegen으로 {len(pending)}장 생성 후 resume")
            raise PendingImages(manifest, len(pending))
        bad = [e for e in entries if e["kind"] == "ai" and e["status"] == "error"]
        if bad:
            raise RuntimeError(f"요청한 AI 이미지 {len(bad)}장 생성 실패: {bad[0].get('error')}")
        c.sub("AI 이미지", "done", f"{sum(e['kind'] == 'ai' for e in entries)}장 검증")
        if gen_ok and real and c.opt.get("image_quality_check", True):
            _quality_pass(c, entries)
    # 2-2) 자극적인 그림(피·부상·시신·선정적 …)에 흑백/흐림 — 캡처 카드·대상 사진·AI 이미지를 한 번에 훑는다
    _sensitive_pass(c, entries)
    # Local grayscale/blur processing can change the PNG bytes. Record the
    # final verified bytes so the manifest and subsequent cache checks agree.
    for e in entries:
        if e.get("engine") == codex_imagegen.PROVIDER and e.get("status") == "ok":
            verified = codex_imagegen.request(e["prompt"], c.dir / e["file"])
            if not verified["ok"]:
                e["status"] = "awaiting_images"
                c.write_json("assets/assets.json", entries)
                raise PendingImages(manifest, 1)
            e["sha256"] = verified["sha256"]
    # 3) 이미지 검사 (Clef 있으면)
    c.sub("이미지 검사", "running")
    checked = 0
    for e in entries:
        if e["kind"] == "ai" and e["status"] in ("ok",):
            try:
                info = c.deps.clef_inspect(c.dir / e["file"], "ai")
            except Exception:  # noqa: BLE001
                info = None
            e["inspect"] = info
            if info:
                checked += 1
                bad = (info.get("text_or_logo") or 0) >= 0.5 or (info.get("real_person_like") or 0) >= 0.5
                if bad:
                    e["recommend_regenerate"] = True
                    why = "글자·로고 또는 실존 인물처럼 보임 — 다시 만들기 추천"
                    e["review_reason"] = f"{e['review_reason']} / {why}" if e.get("review_reason") else why
    c.sub("이미지 검사", "done", f"{checked}장 점검" if checked else "자동 검사 없음 — Codex에서 직접 확인 필요")
    usable = [e for e in entries if e["status"] != "error"]
    if not usable:
        raise RuntimeError("쓸 수 있는 그림이 하나도 없습니다")
    c.write_json("assets/assets.json", entries)
    c.job.artifact("images", [e["file"] for e in usable])
    c.job.artifact("assets", "assets/assets.json")
    c.progress(1.0, f"자료 화면 끝 — {len(usable)}장")


# ------------------------------------------------------------------ 단계: scenes
def load_assets(job: Job) -> list[dict]:
    p = job.dir / "assets" / "assets.json"
    entries = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else []
    return [e for e in entries if e.get("status") in ("ok", "reused") and (job.dir / e["file"]).is_file()]


def build_plan(job: Job, deps: Deps, ctx: Ctx | None = None) -> dict:
    from recap_shorts.images import arrange
    script = json.loads((job.dir / "script_final" / "script.json").read_text(encoding="utf-8"))
    timings = json.loads((job.dir / "voice" / "timings.json").read_text(encoding="utf-8"))
    infos = sentence_infos(script, timings)
    # 아이디·얼굴이 있을 수 있는데 가린 곳이 없는 캡처는 자동 배치에서 뺀다(편집기 후보에는 남음 — 가린 뒤 직접 넣기).
    # 위치 찾기 답이 깨지면 아이디가 그대로인 캡처가 자동으로 들어갈 수 있다.
    images = [e for e in load_assets(job) if not e.get("unblurred_risk")]
    if not images:
        raise RuntimeError("배치할 그림이 없습니다")
    topic_p = job.dir / "topic.json"
    attachments = json.loads(topic_p.read_text(encoding="utf-8")).get("attachments") if topic_p.is_file() else None
    heur = arrange.heuristic_scores(infos, images, attachments)
    clef = None
    if job.options.get("clef_scene_fit"):
        try:
            clef = deps.clef_fit_matrix([i["text"] for i in infos], [job.dir / e["file"] for e in images])
        except Exception as e:  # noqa: BLE001
            job.log(f"[scenes] Clef 적합도 실패(규칙으로 배치): {errors.reason(e)}")
    drop = arrange.off_script(infos, images, attachments)
    scores = arrange.blend(heur, clef, drop)
    sents = timings["sentences"]
    first_cut = next((s["start"] for s in sents if s.get("role") != "hook"), sents[0]["end"])
    counts = []
    for n, s in enumerate(sents):
        end = first_cut if s.get("role") == "hook" else (sents[n + 1]["start"] if n + 1 < len(sents) else timings["duration"])
        start = 0.0 if s.get("role") == "hook" else s["start"]
        counts.append(arrange.shots_for(end - start, s.get("role") == "hook"))
    kinds = [arrange.kind_of(e) for e in images]
    pos = {s["index"]: n for n, s in enumerate(sents)}
    hook_pos = next((n for n, s in enumerate(sents) if s.get("role") == "hook"), None)
    owners = [pos.get(e.get("sentence")) if k == "focus" else (hook_pos if k == "dc_head" else None)
              for e, k in zip(images, kinds)]
    pick = arrange.plan_with_ai_cap(scores, counts, kinds, owners, float(job.options.get("max_ai_share", 0.34)))
    plan = {str(s["index"]): [images[j]["file"] for j in pick[n]] for n, s in enumerate(sents)}
    sc = {str(s["index"]): {images[j]["file"]: round(scores[n][j], 3) for j in range(len(images))} for n, s in enumerate(sents)}
    used = [j for lst in pick for j in lst]
    caps = sum(1 for j in used if kinds[j] != "ai")
    mix = f"캡처 {caps}컷 · AI {len(used) - caps}컷" + (f" · 대본이 안 다루는 이야기의 첨부 캡처 {len(drop)}장 뺌" if drop else "")
    return {"plan": plan, "focus": {}, "sources": {},
            "scores": sc, "scored_by": "clef" if clef else "heuristic",
            "note": (f"자동 배치(Clef 적합도 + 규칙, 캡처 먼저) — {mix}" if clef
                     else f"자동 배치(규칙: 캡처 먼저, AI 는 문장당 1장까지 끝에) — {mix}")}


def write_timeline(job: Job) -> dict:
    from recap_shorts import timeline
    script = json.loads((job.dir / "script_final" / "script.json").read_text(encoding="utf-8"))
    timings = json.loads((job.dir / "voice" / "timings.json").read_text(encoding="utf-8"))
    doc = json.loads((job.dir / "shot_plan.json").read_text(encoding="utf-8"))
    plan = doc["plan"]
    for k, files in plan.items():
        if not files:
            raise ValueError(f"문장 {k} 에 배치된 그림이 없습니다")
        for f in files:
            if not (job.dir / f).is_file():
                raise ValueError(f"그림 파일이 없습니다: {f}")
    from recap_shorts import wordcuts   # 문장 안 그림 컷을 어절 경계에 놓는다(config/fx.json timing.cuts_at_word_gaps, 꺼지면 None)
    tl = timeline.build(script, timings, plan, voice="voice/voice.wav", bgm=None, focus=doc.get("focus"),
                        sources=doc.get("sources"), word_cuts=wordcuts.from_options(job.dir, timings, job.options),
                        cuts=doc.get("cuts"))
    from recap_shorts.fx import maybe_apply  # 짤·효과음: 꺼져 있으면(기본) tl 을 그대로 돌려준다
    tl = maybe_apply(job.dir, tl, timings, job.options)
    (job.dir / "timeline.json").write_text(json.dumps(tl, ensure_ascii=False, indent=1), encoding="utf-8")
    return tl


def step_scenes(c: Ctx) -> None:
    c.progress(0.2, "그림과 문장의 어울림을 계산하는 중")
    doc = build_plan(c.job, c.deps, c)
    c.write_json("shot_plan.json", doc)
    c.progress(0.8, "타임라인을 만드는 중")
    tl = write_timeline(c.job)
    c.job.artifact("shot_plan", "shot_plan.json")
    c.job.artifact("timeline", "timeline.json")
    mix = doc["note"].rsplit("— ", 1)[-1] if "— " in doc["note"] else ""
    c.sub("장면 배치", "done", f"{doc['scored_by']} · 샷 {len(tl['shots'])}개" + (f" · {mix}" if mix else ""))
    c.progress(1.0, f"장면 배치 끝 — 샷 {len(tl['shots'])}개, {tl['duration']:.1f}초")


# ------------------------------------------------------------------ 단계: render
def step_render(c: Ctx) -> None:
    c.progress(0.02, "타임라인을 다시 만드는 중")
    tl = write_timeline(c.job)
    c.sub("타임라인", "done", f"샷 {len(tl['shots'])}개")
    c.sub("렌더", "running")
    out = c.dir / "out" / "final.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    (out.parent / "upload_sound.txt").unlink(missing_ok=True)   # 지난 렌더의 업로드 음악 안내가 남지 않게(이번에 배경음악 요청이 있으면 렌더러가 다시 씀)
    t0 = time.time()
    res = c.deps.render(c.dir / "timeline.json", out,
                        progress=lambda p: c.progress(0.05 + 0.93 * p, f"영상 렌더링 {p:.0%}"))
    c.sub("렌더", "done", f"{time.time() - t0:.0f}초 · {res.get('duration', tl['duration']):.1f}초 분량")
    c.job.artifact("video", "out/final.mp4")
    if res.get("upload_sound"):   # 배경음악은 영상에 굽지 않고 올릴 때 넣는다(config/fx.json bgm_mode)
        c.job.artifact("upload_sound", "out/upload_sound.txt")
    elif "upload_sound" in c.job.state["artifacts"]:
        with c.job.lock:
            c.job.state["artifacts"].pop("upload_sound", None)
            c.job.save()
    c.progress(1.0, "영상 완성")


# ------------------------------------------------------------------ 단계: review
def _final_timeline(job: Job) -> dict:
    """영상에 쓴 타임라인(fx 명령으로 따로 만든 timeline_fx.json 이 있으면 그것). 없거나 깨졌으면 {}."""
    tl_path = job.dir / "timeline_fx.json"
    if not tl_path.is_file():
        tl_path = job.dir / "timeline.json"
    try:
        return json.loads(tl_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def sfx_credits(job: Job) -> list[str]:
    """영상에 실제로 들어간 효과음 중 출처 표기가 필요한 것(OtoLogic CC BY 4.0, ポケットサウンド 규약 등)의 표기 문구.
    효과음 manifest 항목의 credit 이 "필수: …" 이면 그 뒤 문구를 쓴다(효과음 표기 규칙).
    manifest 는 쓴 효과음 파일이 들어 있던 폴더(기본 꾸러미·설정에서 고른 폴더 어디든)에서 찾는다."""
    tl = _final_timeline(job)
    files = {str(Path(e.get("file", ""))).replace("\\", "/") for e in (tl.get("audio") or {}).get("sfx") or []}
    if not files:
        return []
    out: list[str] = []
    manifests = sorted({str(Path(f).parent / "manifest.json") for f in files})
    for mf in (Path(m) for m in manifests):
        if not mf.is_file():
            continue
        try:
            items = json.loads(mf.read_text(encoding="utf-8")).get("items", [])
        except (OSError, ValueError):
            continue
        for it in items:
            f = str(mf.parent / str(it.get("file", ""))).replace("\\", "/")
            cr = str(it.get("credit") or "")
            if f in files and cr.startswith("필수"):
                text = cr.split(":", 1)[1].strip() if ":" in cr else cr
                if text not in out:
                    out.append(text)
    return out


def sfx_rights_warnings(tl: dict) -> list[dict]:
    """영상에 들어간 효과음 중 저작권 위험이 medium 이상인 것 [{t, id, level, note, source}] — 검수 화면 경고용(막지 않음).
    config/sfx_rights.json 의 기록(원본 파일 이름만 보고 매긴 판단)을 쓰고, 읽기 실패는 경고 없음으로 둔다."""
    try:
        from recap_shorts.fx import rights
        return rights.warnings_for_timeline(tl)
    except Exception:  # noqa: BLE001
        return []


def _no_handles(text: str) -> str:
    """설명에는 유튜브 채널 핸들·주소를 쓰지 않는다."""
    text = re.sub(r"https?://(?:www\.|m\.)?(?:youtube\.com|youtu\.be)\S*", "", text)
    text = re.sub(r"(?<![\w.])@[\w.\-]+", "", text)
    return re.sub(r"\s{2,}", " ", text).strip(" ,")


def meme_credits(job: Job) -> list[str]:
    """영상에 실제로 나온 짤(전체화면 샷 kind="meme"·스티커 오버레이)의 이름·출처·라이선스. 짤 폴더의 manifest.json 에서 찾는다.
    공정 이용 이미지를 쓸 때 출처는 영상 설명에 남긴다."""
    tl = _final_timeline(job)
    shown = [x for x in (tl.get("shots") or []) + (tl.get("overlays") or []) if x.get("kind") == "meme" or x.get("meme_id")]
    out: list[str] = []
    seen: set[str] = set()
    libs: dict[Path, list[dict]] = {}
    for x in sorted(shown, key=lambda x: x.get("start", 0)):
        # 짤은 작업 폴더 assets/meme/ 에 복사돼 있고 원래 자리(manifest 가 있는 라이브러리)는 meme_src
        f = Path(str(x.get("meme_src") or x.get("file") or ""))
        if not f.is_absolute():
            f = next((q for q in (job.dir / f, ROOT / f) if q.is_file()), ROOT / f)
        mid = str(x.get("meme_id") or f.stem)
        if mid in seen:
            continue
        seen.add(mid)
        if f.parent not in libs:
            try:
                doc = json.loads((f.parent / "manifest.json").read_text(encoding="utf-8"))
                libs[f.parent] = (doc.get("items", []) if isinstance(doc, dict) else doc) or []
            except (OSError, ValueError):
                libs[f.parent] = []
        it = next((i for i in libs[f.parent] if i.get("id") == mid or i.get("file") == f.name), {})
        # 공개 설명이라 내부 기록('사용자 제공(… 채팅에 붙여 넣음)', 'unknown (… 확인 못 함)' 같은 메모)은 빼고
        # 출처 사이트·주소와 캐릭터 원작자만 적는다(설명에 내부 메모가 그대로 나가면 안 된다)
        site = str(it.get("source_site") or "")
        if site.startswith("사용자 제공"):
            site = ""
        src = _no_handles(" ".join(str(v) for v in (site, it.get("source_url")) if v))
        lic = _no_handles(str(it.get("license") or ""))
        rest = [f"출처 {src}"] if src else []
        if "Matt Furie" in lic:
            rest.append("캐릭터 원작 Matt Furie")
        elif lic and not lic.lower().startswith("unknown") and len(lic) <= 40:
            rest.append(f"라이선스 {lic}")
        name = _no_handles(str(it.get("name") or mid))
        out.append(f"{name} — {' · '.join(rest)}" if rest else name)
    return out


def make_description(job: Job) -> str:
    src = job.state["source"]
    script = json.loads((job.dir / "script_final" / "script.json").read_text(encoding="utf-8"))
    topic = json.loads((job.dir / "topic.json").read_text(encoding="utf-8"))
    entries = load_assets(job)
    plan = json.loads((job.dir / "shot_plan.json").read_text(encoding="utf-8"))["plan"]
    used = {f for fs in plan.values() for f in fs}
    ai_used = any(e["kind"] == "ai" and e["file"] in used for e in entries)
    atts = [a for a in topic.get("attachments", []) if a.get("kind") and a["kind"] != "읽기 실패"]
    # 영상에 실제로 나온 첨부만 적는다(빼 둔 뉴스 캡처·팬 그림까지 '포함'이라고 적히면 안 된다). 캡처 카드의 attachment 번호로 찾는다.
    shown = {e.get("attachment") for e in entries if e["kind"] == "cap" and e["file"] in used and e.get("attachment")}
    if shown:
        atts = [a for a in atts if a.get("n") in shown]
    local = site_of(src) == "local"
    where = str(topic.get("source_label") or post_label(src)) if local else post_label(src)
    date = (src.get("written_at") or "")[:10]
    source_title = str(topic.get("topic") or src.get("title") or "제목 없는 자료") if local else str(src.get("title") or "")
    source_url = str(topic.get("source_url") or src.get("url") or "") if local else str(src.get("url") or "")
    lines = [" ".join(script.get("title_lines") or [job.state["title"]]), "", "[출처]",
             f"- {where}{'' if local else ' 게시글'} 「{source_title}」" + (f" ({date})" if date else "")]
    if source_url:
        lines.append(f"  {source_url}")
    if atts:
        kinds = ", ".join(dict.fromkeys(("팬이 만든 " if a.get("fan_made") else "") + a["kind"] for a in atts))
        lines.append(f"  · 게시글 첨부 이미지: {kinds}")
    refs = [e for e in entries if e.get("ref") and e["file"] in used]
    for e in refs:
        r = e["ref"]
        lines.append(f"  · 참고 이미지 「{r.get('name')}」: {r.get('site')} {r.get('page_url')} ({r.get('licence')}{', ' + r['artist'] if r.get('artist') else ''})")
    memes = meme_credits(job)
    if memes:
        lines += ["", "[짤]"] + [f"- {m}" for m in memes]
    credits = sfx_credits(job)
    if credits:
        lines += ["", "[효과음]"] + [f"- {c}" for c in credits]
    notes = ["내용은 사용자가 제공한 자료를 기준으로 작성했습니다." if local
             else "내용은 위 커뮤니티 게시글 기준이며 공식 기록과 대조하지 않았습니다."]
    if any(a.get("fan_made") for a in atts):
        notes[0] = "내용은 위 커뮤니티 게시글 기준이며, 팬이 만든 자료(확인되지 않음)가 포함돼 있습니다."
    if ai_used:
        notes.append("일부 장면은 AI로 생성한 이미지입니다(실제 인물·현장 사진이 아님).")
    notes.append("내레이션은 로컬 모델로 합성한 AI 음성입니다.")
    lines += ["", "[알림]"] + [f"- {n}" for n in notes]
    return "\n".join(lines) + "\n"


def sensitive_pictures(job: Job, tl: dict | None = None) -> list[dict]:
    """영상에 쓴 그림 중 자극적이라고 판정돼(level mild/strong) 처리했거나 처리 여부를 사람이 정한 것 [{file, level, kind, reason, treatment, user}].
    검수 화면에는 지금 assets.json 상태를 다시 읽어 쓴다(검수 뒤에 사람이 바꿀 수 있어서) — 이 목록은 그 시점의 기록."""
    tl = tl if tl is not None else _final_timeline(job)
    used = {x.get("file") for x in tl.get("shots") or []}
    out = []
    for e in load_assets(job):
        s = e.get("sensitive") or {}
        if e["file"] in used and s.get("level") in ("mild", "strong"):
            out.append({"file": e["file"], "level": s["level"], "kind": s.get("kind"), "reason": s.get("reason"),
                        "treatment": s.get("treatment"), "user": bool(s.get("user"))})
    return out


def step_review(c: Ctx) -> None:
    c.progress(0.1, "설명 문구를 만드는 중")
    desc = make_description(c.job)
    p = c.dir / "out" / "description.txt"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(desc, encoding="utf-8")
    c.job.artifact("description", "out/description.txt")
    c.sub("설명 문구", "done")
    script = c.read_json("script_final/script.json")
    check = c.read_json("script_final/script_check.json")
    tm = c.read_json("voice/timings.json")
    tl = c.read_json("timeline.json")
    review = {"duration": tl["duration"], "shots": len(tl["shots"]), "captions": len(tl["captions"]),
              "first_cut": tl["hook"]["end"], "syllables": check["checks"]["syllables"]["total"],
              "speech_rate": tm.get("rate"), "asr_mean_match": tm.get("asr_mean_match"),
              "script_flags": check.get("flags", []), "clef": None}
    c.progress(0.4, "Codex에서 확인할 검수 자료를 정리하는 중")
    c.sub("영상 점검", "running")
    topic_line = " ".join(script.get("title_lines") or [])
    try:
        w = c.deps.clef_watch(c.dir / "out" / "final.mp4", topic_line)
    except Exception as e:  # noqa: BLE001
        w = None
        c.log(f"영상 점검 실패: {errors.reason(e)}")
    review["clef"] = w
    c.sub("영상 점검", "done", "점검 결과 있음(참고용)" if w else "자동 검사 없음 — Codex에서 직접 확인 필요")
    review["needs_review_assets"] = [e["file"] for e in load_assets(c.job) if e.get("needs_review") or e.get("recommend_regenerate")]
    review["sfx_rights"] = sfx_rights_warnings(tl)
    review["sensitive"] = sensitive_pictures(c.job, tl)
    c.write_json("out/review.json", review)
    c.job.artifact("review", "out/review.json")
    c.progress(1.0, "검수 준비 끝 — 영상을 보고 승인하세요")


STEP_FUNCS = {"source": step_source, "read": step_read, "script": step_script, "voice": step_voice,
              "assets": step_assets, "scenes": step_scenes, "render": step_render, "review": step_review}


# ------------------------------------------------------------------ 실행기
class Runner:
    """백그라운드 스레드 1개 큐 — job 은 동시에 하나만 돈다."""

    def __init__(self, store: JobStore, deps: Deps | None = None):
        self.store, self.deps = store, deps or Deps()
        self.q: queue.Queue = queue.Queue()
        self.thread: threading.Thread | None = None
        self.stopping = False

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stopping = False
        self.thread = threading.Thread(target=self._loop, name="recap-runner", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stopping = True
        self.q.put(None)

    def _loop(self) -> None:
        while True:
            item = self.q.get()
            try:
                if item is None or self.stopping:
                    return
                job_id, fn = item
                job = self.store.get(job_id)
                if job is None:
                    continue
                try:
                    (fn or self.run_job)(job)
                except Exception as e:  # noqa: BLE001
                    write_error_log(job, "runner", e)
                    job.log(f"작업 오류: {errors.screen_text(e)}")
                    job.set_status("error")
            finally:
                self.q.task_done()

    def submit(self, job: Job, fn=None, detail: str | None = None) -> None:
        job.set_status("running")
        if detail:
            job.log(detail)
        self.q.put((job.id, fn))

    def wait_idle(self, timeout: float = 60.0) -> bool:
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.q.unfinished_tasks == 0:
                return True
            time.sleep(0.02)
        return False

    # -- 한 job 을 멈출 곳까지
    def run_job(self, job: Job) -> None:
        for key in STEP_KEYS:
            st = job.step(key)
            if st["status"] == "done":
                continue
            if st["status"] == "waiting":
                job.set_status("waiting")
                return
            job.update_step(key, status="running", started_at=now(), ended_at=None, error=None, progress=0.0,
                            detail="시작하는 중")
            job.set_status("running")
            job.log(f"{st['label']} 시작")
            ctx = Ctx(job, key, self.deps)
            t0 = time.time()
            try:
                STEP_FUNCS[key](ctx)
            except PendingImages as e:
                job.update_step(key, status="awaiting_images", ended_at=None, error=None,
                                detail=f"Codex imagegen {e.count}장 대기")
                job.artifact("imagegen_tasks", rel(job, e.manifest))
                job.log(f"Codex imagegen 파일을 저장한 뒤 resume: {rel(job, e.manifest)}")
                job.set_status("awaiting_images")
                return
            except Exception as e:  # noqa: BLE001
                msg = report_error(job, f"{key}({st['label']})", e)
                job.update_step(key, status="error", ended_at=now(), error=msg, detail="오류로 멈춤")
                job.log(f"{st['label']} 오류: {msg}")
                job.set_status("error")
                return
            auto = set(job.options.get("auto_steps") or [])
            if key in CONFIRM_STEPS and key not in auto:
                job.update_step(key, status="waiting", ended_at=now(), progress=1.0)
                job.log(f"{st['label']} 끝 ({time.time() - t0:.0f}초) — 확인을 기다리는 중")
                job.set_status("waiting")
                return
            job.update_step(key, status="done", ended_at=now(), progress=1.0)
            job.log(f"{st['label']} 끝 ({time.time() - t0:.0f}초)")
        job.set_status("done")
        job.log("모든 단계 끝")



class Busy(RuntimeError):
    pass


def continue_job(runner: Runner, job: Job) -> None:
    """waiting 단계를 done 으로 넘기고 다음으로, error 면 그 단계부터 다시. 실행 중이면 Busy."""
    with job.lock:
        if job.state["status"] == "running":
            raise Busy("지금 작업 중입니다")
        if job.state["status"] == "done":
            return
        for s in job.state["steps"]:
            if s["status"] == "waiting":
                s.update(status="done", ended_at=s.get("ended_at") or now())
                break
        job.save()
    runner.submit(job, detail="이어서 진행")
