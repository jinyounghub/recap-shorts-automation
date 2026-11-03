"""효과음·짤 폴더 분석 꾸러미: 보관 자리, 백그라운드 실행(한 번에 하나), 화면용 모양, 사람이 고친 것 반영 (docs/library_analysis.md).

꾸러미 = assets_local/analyzed/<kind>/<id>/ (analysis.json + manifest.json + 만든 파일). 원본 폴더에는 쓰지 않는다.
설정의 효과음·짤 폴더 목록에 꾸러미 폴더를 넣으면 영상이 그 manifest 로 고른다.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from . import analyze_meme as AM
from . import analyze_sfx as AS
from . import sfx as S
from .config import ROOT, load_config

KINDS = ("sfx", "meme")
RUNNING_STATES = ("queued", "measuring", "listening", "building")
ID_RE = re.compile(r"^[\w.\-]{1,64}$")
BATCH = 6
WORKERS = 4
_SAVE_EVERY_S = 1.0


class LibraryError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def root() -> Path:
    return Path(os.environ.get("RECAP_LIBRARY_DIR") or ROOT / "assets_local" / "analyzed")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def pack_id(src: Path) -> str:
    name = re.sub(r"[^\w\-]+", "_", src.name, flags=re.U).strip("_")[:40] or "folder"
    return f"{name}-{hashlib.sha1(str(src).lower().encode('utf-8')).hexdigest()[:6]}"


def _kind(kind: str) -> str:
    if kind not in KINDS:
        raise LibraryError(f"모르는 종류입니다: {kind}")
    return kind


def pack_dir(kind: str, pid: str) -> Path:
    if not ID_RE.match(pid or "") or pid in (".", ".."):
        raise LibraryError("꾸러미 이름이 맞지 않습니다", 404)
    return root() / _kind(kind) / pid


def read(kind: str, pid: str) -> dict | None:
    try:
        doc = json.loads((pack_dir(kind, pid) / "analysis.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def write(doc: dict) -> None:
    d = pack_dir(doc["kind"], doc["id"])
    d.mkdir(parents=True, exist_ok=True)
    doc["updated_at"] = _now()
    tmp = d / f"analysis.json.{threading.get_ident()}.tmp"
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    # Windows 는 화면이 1.5초마다 읽는 순간과 겹치면 바꿔 넣기가 '액세스 거부'로 실패한다 — 잠깐 쉬고 다시
    for n in range(5):
        try:
            os.replace(tmp, d / "analysis.json")
            return
        except PermissionError:
            if n == 4:
                raise
            time.sleep(0.05 * (n + 1))


def pack_of(path: str | Path) -> dict | None:
    """설정 폴더가 분석 꾸러미면 {"kind","id","source_dir"}."""
    try:
        rel = Path(path).resolve().relative_to(root().resolve())
    except (ValueError, OSError):
        return None
    if len(rel.parts) != 2 or rel.parts[0] not in KINDS:
        return None
    doc = read(rel.parts[0], rel.parts[1])
    return {"kind": rel.parts[0], "id": rel.parts[1], "source_dir": doc.get("source_dir")} if doc else None


def _typed(d: str) -> str:
    return str(d or "").strip().strip('"')


def resolve_dir(d: str) -> Path:
    """빈 칸이면 오류(Path("") 는 '.' 이라 설치 폴더 전체가 된다). 오류 문구에는 친 그대로를 쓴다(_typed)."""
    s = _typed(d)
    if not s:
        raise LibraryError("폴더를 넣으세요")
    p = Path(os.path.expandvars(os.path.expanduser(s)))
    return (p if p.is_absolute() else ROOT / p).resolve()


# ------------------------------------------------------------------ 실행

_lock = threading.Lock()
_running: dict | None = None     # {"kind","id","stop": Event, "thread"}


def running() -> dict | None:
    r = _running
    if r and r["thread"] is not None and not r["thread"].is_alive():
        return None
    return {"kind": r["kind"], "id": r["id"]} if r else None


def _is_running(kind: str, pid: str) -> bool:
    r = running()
    return bool(r and r["kind"] == kind and r["id"] == pid)


def _files(kind: str, src: Path, cfg: dict) -> tuple[list[Path], int]:
    if kind == "sfx":
        return AS.list_files(src, int(AS.opts(cfg)["max_files"]), skip=root())
    return AM.list_images(src, int(AM.opts(cfg)["max_files"]), skip=root())


def start(kind: str, folder: str, *, use: bool = False, llm: Optional[Callable] = None, wait: bool = False) -> dict:
    """분석을 시작한다(같은 폴더면 예전 결과를 이어 씀). wait=True 면 끝날 때까지 기다린다(시험·명령줄)."""
    global _running
    _kind(kind)
    src = resolve_dir(folder)
    if not src.is_dir():
        raise LibraryError(f"폴더가 없습니다: {_typed(folder)}")
    try:
        src.relative_to(root().resolve())
        raise LibraryError("이미 분석해서 만든 꾸러미 폴더입니다 — 원래 폴더를 고르세요")
    except ValueError:
        pass
    cfg = load_config()
    files, _ = _files(kind, src, cfg)
    if not files:
        raise LibraryError(f"이 폴더에서 {'소리 파일(wav·mp3·ogg·flac·m4a)' if kind == 'sfx' else '그림 파일(png·jpg·webp·gif)'}을 찾지 못했습니다: {_typed(folder)}")
    pid = pack_id(src)
    with _lock:
        if running():
            raise LibraryError("다른 폴더를 분석하는 중입니다 — 끝나거나 멈춘 뒤에 다시 누르세요", 409)
        doc = read(kind, pid) or {"version": 1, "kind": kind, "id": pid, "created_at": _now(), "items": []}
        doc.update({"source_dir": str(src), "state": "queued", "message": None, "use_on_done": bool(use),
                    "progress": {"done": 0, "total": len(files), "step": "기다리는 중"}})
        write(doc)
        stop = threading.Event()
        _running = {"kind": kind, "id": pid, "stop": stop, "thread": None}
        th = threading.Thread(target=_run, args=(kind, pid, stop, llm), daemon=True, name=f"library-{kind}")
        _running["thread"] = th
        th.start()
    if wait:
        th.join()
    return summary(read(kind, pid) or doc)


def stop(kind: str, pid: str) -> dict:
    r = _running
    late = False
    if r and r["kind"] == kind and r["id"] == pid:
        r["stop"].set()
        if r["thread"] is not None:
            r["thread"].join(timeout=30)
            late = r["thread"].is_alive()
    doc = read(kind, pid)
    if doc is None:
        raise LibraryError("꾸러미가 없습니다", 404)
    out = summary(doc)
    if late:     # Codex 에 이미 보낸 묶음은 답(최대 몇 분)을 받아야 끝난다 — 남은 묶음은 보내지 않는다
        out["message"] = "멈추는 중입니다 — 이미 보낸 것의 답이 오면 멈춥니다"
    return out


class _Stopped(Exception):
    pass


def _progress(doc: dict, step: str, done: int, total: int, last: list) -> None:
    doc["progress"] = {"done": done, "total": total, "step": step}
    if time.monotonic() - last[0] >= _SAVE_EVERY_S or done >= total:
        write(doc)
        last[0] = time.monotonic()


def _merge_items(doc: dict, src: Path, files: list[Path]) -> list[dict]:
    """파일 목록에 예전 결과를 붙인다 — 크기·수정 시각이 같으면 잰 값·들은 결과를 그대로, 사람 값은 언제나 남긴다."""
    old = {it["key"]: it for it in doc.get("items", []) if isinstance(it, dict) and it.get("key")}
    items = []
    for p in files:
        key = p.relative_to(src).as_posix()
        sig = AS.file_sig(p)
        prev = old.get(key)
        if prev and prev.get("sig") == sig:
            it = dict(prev)
        else:
            it = {"key": key, "sig": sig, "user": dict((prev or {}).get("user") or {})}
        items.append(it)
    return items


def _codex_ready() -> bool:
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("RECAP_FX_NO_LLM"):
        return False
    try:
        from recap_shorts.llm.codex import is_available
        return is_available()
    except Exception:  # noqa: BLE001
        return False


def _stop_on(e: Exception) -> bool:
    """이 오류면 남은 묶음도 같은 결과라 듣기를 멈춘다(키·한도)."""
    from recap_shorts import errors
    return errors.kind_of(e) in (errors.NO_KEY, errors.BAD_KEY, errors.QUOTA)


def _run(kind: str, pid: str, stop_ev: threading.Event, llm) -> None:
    global _running
    doc = read(kind, pid) or {}
    try:
        cfg = load_config()
        src = Path(doc["source_dir"])
        pack = pack_dir(kind, pid)
        pack.mkdir(parents=True, exist_ok=True)
        files, extra = _files(kind, src, cfg)
        doc["items"] = _merge_items(doc, src, files)
        notes = [f"파일이 많아 앞의 {len(files)}개만 봤습니다(뺀 파일 {extra}개)"] if extra else []
        last = [0.0]
        if kind == "sfx":
            notes += _run_sfx(doc, src, pack, cfg, stop_ev, llm, last)
        else:
            notes += _run_meme(doc, src, pack, cfg, stop_ev, llm, last)
        doc["state"] = "done"
        doc["message"] = " · ".join(notes) or None
        doc["progress"] = {"done": len(doc["items"]), "total": len(doc["items"]), "step": "끝"}
        write(doc)
        if doc.get("use_on_done"):
            try:
                use(kind, pid, True)
            except LibraryError as e:
                doc["message"] = " · ".join([*notes, f"영상에 쓰도록 넣지 못함: {e}"])
            doc["use_on_done"] = False
            write(doc)
    except _Stopped:
        doc["state"] = "stopped"
        doc["message"] = "멈췄습니다 — 다시 분석을 누르면 이어서 합니다"
        doc["use_on_done"] = False
        write(doc)
    except Exception as e:  # noqa: BLE001
        from recap_shorts import errors
        doc["state"] = "error"
        doc["message"] = errors.screen_text(e, "분석하다 멈췄습니다")
        doc["use_on_done"] = False
        if doc.get("kind"):
            write(doc)
    finally:
        with _lock:
            if _running and _running["kind"] == kind and _running["id"] == pid:
                _running = None


def _listen_all(doc: dict, todo: list[dict], call: Callable[[list[dict]], dict], step: str,
                stop_ev: threading.Event, last: list) -> list[str]:
    """todo 를 6개씩 나눠 동시에 4묶음까지 묻는다. 키·한도 오류면 남은 묶음은 묻지 않는다. 반환: 메모."""
    batches = [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]
    state = {"done": 0, "halt": None}
    lk = threading.Lock()

    def one(batch):
        if stop_ev.is_set() or state["halt"]:
            return
        try:
            got = call(batch)
            err = None
        except Exception as e:  # noqa: BLE001
            got, err = {}, e
        with lk:
            for it in batch:
                if it["key"] in got:
                    it[step] = got[it["key"]]
                    it.pop("listen_error", None)
                    it.pop("look_error", None)
                else:
                    it["listen_error" if step == "heard" else "look_error"] = True
            if err is not None and _stop_on(err) and not state["halt"]:
                from recap_shorts import errors
                state["halt"] = errors.screen_text(err, "Codex 요청이 막혔습니다")
            state["done"] += len(batch)
            _progress(doc, "듣는 중" if step == "heard" else "보는 중", state["done"], len(todo), last)

    with ThreadPoolExecutor(WORKERS) as ex:
        list(ex.map(one, batches))
    if stop_ev.is_set():
        raise _Stopped()
    missed = sum(1 for it in todo if step not in it or it.get(step) is None)
    notes = []
    if state["halt"]:
        notes.append(f"Codex 를 끝까지 쓰지 못함({state['halt']}) — 다시 분석하면 남은 것만 이어서 합니다")
    elif missed:
        notes.append(f"{missed}개는 답을 받지 못함 — 다시 분석하면 그것만 다시 묻습니다")
    return notes


def _run_sfx(doc, src, pack, cfg, stop_ev, llm, last) -> list[str]:
    o = AS.opts(cfg)
    types = AS.wanted_types(cfg.get("sfx") or {})
    items = doc["items"]
    for it in items:
        it["name_type"] = S.type_from_name(Path(it["key"]))
    # 비슷한 소리 찾기 표(sig)가 없는 예전 측정도 다시 잰다(무료, 이 컴퓨터에서)
    todo = [it for it in items if "measure" not in it or not (it["measure"].get("error") or it["measure"].get("sig"))]
    doc["state"] = "measuring"
    done = [0]
    lk = threading.Lock()

    def meas(it):
        if stop_ev.is_set():
            return
        p = src / it["key"]
        m = AS.measure(p)
        try:
            h = AS.content_hash(p)
        except OSError:
            h = None
        with lk:      # 저장(_progress → write)이 같은 잠금 안에서 doc 을 읽으므로 고치는 것도 잠금 안에서
            it["measure"], it["hash"] = m, h
            done[0] += 1
            _progress(doc, "재는 중", done[0], len(todo), last)

    if todo:
        _progress(doc, "재는 중", 0, len(todo), last)
        with ThreadPoolExecutor(WORKERS) as ex:
            list(ex.map(meas, todo))
    if stop_ev.is_set():
        raise _Stopped()

    notes: list[str] = []
    # 종류 목록이 바뀌면(설정의 자리가 바뀜) 어울림 점수를 다시 받는다
    if doc.get("types") != types:
        for it in items:
            it.pop("heard", None)
    doc["types"] = types
    model = (cfg.get("analyze") or {}).get("model") or None
    if model and str(model).lower().startswith("gemini"):
        model = None
    if llm is not None:
        doc["state"] = "listening"
        todo = [it for it in items if it.get("heard") is None and not (it.get("measure") or {}).get("error")
                and float((it.get("measure") or {}).get("dur", 0)) <= float(o["max_file_s"])]
        _progress(doc, "듣는 중", 0, len(todo), last)
        notes += _listen_all(doc, todo, lambda b: AS.listen_batch(
            [(it["key"], src / it["key"], float(it["measure"].get("onset", 0))) for it in b], types,
            model=model, listen_s=float(o["listen_s"]), llm=llm), "heard", stop_ev, last)
        doc["listened"], doc["model"] = True, model
    else:
        doc["listened"], doc["model"] = False, None
        notes.append("음성의 외부 API 분석은 사용하지 않습니다. 로컬 측정값·파일명과 수동 manifest 태그로 종류를 정했습니다")
    doc["state"] = "building"
    _progress(doc, "꾸러미 만드는 중", 0, 1, last)
    AS.decide(items, types, o)
    res = AS.build(doc, pack, src, cfg, types)
    _count(doc, res)
    return notes


def _run_meme(doc, src, pack, cfg, stop_ev, llm, last) -> list[str]:
    o = AM.opts(cfg)
    beats = AM.beats_for(cfg)
    items = doc["items"]
    hash_len = AM.HASH_SIDE * AM.HASH_SIDE // 4     # 예전 크기의 해시로 잰 것은 다시 잰다(무료)
    todo = [it for it in items if "measure" not in it
            or not (it["measure"].get("error") or len(it["measure"].get("dhash") or "") == hash_len)]
    doc["state"] = "measuring"
    for n, it in enumerate(todo, 1):
        if stop_ev.is_set():
            raise _Stopped()
        it["measure"] = AM.measure(src / it["key"])
        _progress(doc, "재는 중", n, len(todo), last)
    notes: list[str] = []
    model = (cfg.get("analyze") or {}).get("model") or None
    if model and str(model).lower().startswith("gemini"):
        model = None
    if llm is not None or _codex_ready():
        doc["state"] = "listening"
        todo = [it for it in items if it.get("seen") is None and not (it.get("measure") or {}).get("error")
                and min(int((it.get("measure") or {}).get("w", 0)), int((it.get("measure") or {}).get("h", 0))) >= int(o["min_side_px"])]
        _progress(doc, "보는 중", 0, len(todo), last)
        notes += _listen_all(doc, todo, lambda b: AM.look_batch(
            [(it["key"], src / it["key"]) for it in b], beats, model=model, llm=llm), "seen", stop_ev, last)
        doc["listened"], doc["model"] = True, model
    else:
        doc["listened"], doc["model"] = False, None
        notes.append("Codex CLI를 사용할 수 없어 파일 이름을 짤의 뜻으로 씁니다")
    doc["state"] = "building"
    _progress(doc, "꾸러미 만드는 중", 0, 1, last)
    AM.decide(items, o)
    res = AM.build(doc, pack, src, cfg)
    _count(doc, res)
    return notes


def _count(doc: dict, res: dict) -> None:
    doc["counts"] = {"files": len(doc["items"]), "kept": res["kept"], "dropped": len(doc["items"]) - res["kept"],
                     "groups": res["groups"]}


# ------------------------------------------------------------------ 화면용 모양

def _used(kind: str, pid: str) -> bool:
    from recap_shorts import settings
    dirs = settings.sfx_dirs() if kind == "sfx" else settings.meme_dirs()
    me = str(pack_dir(kind, pid).resolve())
    return any(str(Path(d).resolve()) == me for d in dirs)


def summary(doc: dict) -> dict:
    kind, pid = doc["kind"], doc["id"]
    state, msg = doc.get("state", "done"), doc.get("message")
    if state in RUNNING_STATES and not _is_running(kind, pid):
        state, msg = "stopped", "프로그램이 꺼져 분석이 멈췄습니다 — 다시 분석을 누르면 이어서 합니다"
    items = doc.get("items") or []
    return {"kind": kind, "id": pid, "source_dir": doc.get("source_dir"), "out_dir": str(pack_dir(kind, pid)),
            "state": state, "progress": doc.get("progress") or {"done": 0, "total": 0, "step": ""},
            "counts": doc.get("counts") or {"files": len(items), "kept": 0, "dropped": 0, "groups": {}},
            "used": _used(kind, pid), "listened": bool(doc.get("listened", False)), "model": doc.get("model"),
            "message": msg, "created_at": doc.get("created_at"), "updated_at": doc.get("updated_at")}


def _sfx_item(it: dict) -> dict:
    keep, typ = AS.effective(it)
    m = it.get("measure") or {}
    return {"key": it["key"], "name": Path(it["key"]).stem, "dur": m.get("dur"), "active_len": m.get("active_len"),
            "heard": it.get("heard"), "keep": keep, "type": typ, "score": it.get("score"),
            "auto": it.get("auto") or {"keep": False, "type": None, "why": []}, "user": it.get("user") or {},
            "out": it.get("out")}


def _meme_item(it: dict) -> dict:
    e = AM.effective(it)
    m = it.get("measure") or {}
    return {"key": it["key"], "w": m.get("w"), "h": m.get("h"), "transparent": bool(m.get("transparent")),
            "dup_of": it.get("dup_of"), "seen": it.get("seen"), **e,
            "auto": it.get("auto") or {"keep": False, "why": []}, "user": it.get("user") or {}, "out": it.get("out")}


def detail(doc: dict) -> dict:
    cfg = load_config()
    out = summary(doc)
    # 분석 중에는 목록을 보내지 않는다(화면은 1.5초마다 진행만 본다, 끝나면 그때 목록)
    items = [] if out["state"] in RUNNING_STATES else (doc.get("items") or [])
    if doc["kind"] == "sfx":
        o = AS.opts(cfg)
        types = doc.get("types") or AS.wanted_types(cfg.get("sfx") or {})
        extra = sorted({t for t in (AS.effective(it)[1] for it in items) if t and t not in types})
        out["groups_def"] = [{"id": t, "label": AS.TYPE_LABEL.get(t, t), "want": int(AS.per_type(o["want"], t))}
                             for t in types + extra]
        out["items"] = [_sfx_item(it) for it in items]
    else:
        out["beats_def"] = [{"id": b, "label": AM.BEAT_LABEL[b]} for b in AM.beats_for(cfg)]
        out["families_def"] = [{"id": f, "label": lab} for f, lab in AM.FAMILY_LABEL.items()]
        out["items"] = [_meme_item(it) for it in items]
    return out


def get(kind: str, pid: str) -> dict:
    doc = read(kind, pid)
    if doc is None:
        raise LibraryError("꾸러미가 없습니다", 404)
    return doc


def list_all() -> dict:
    packs = []
    for kind in KINDS:
        d = root() / kind
        for f in sorted(d.glob("*/analysis.json")) if d.is_dir() else []:
            doc = read(kind, f.parent.name)
            if doc:
                packs.append(summary(doc))
    packs.sort(key=lambda p: p.get("updated_at") or "", reverse=True)
    return {"running": running(), "packs": packs}


# ------------------------------------------------------------------ 사람이 고친 것

def apply_changes(kind: str, pid: str, changes: list) -> dict:
    """화면은 카드마다 따로 저장하므로 두 장을 잇달아 고치면 요청이 겹친다 — 한 번에 하나씩(겹치면 앞의 변경이 사라졌다)."""
    with _lock:
        return _apply_changes(kind, pid, changes)


def _apply_changes(kind: str, pid: str, changes: list) -> dict:
    doc = get(kind, pid)
    if _is_running(kind, pid):
        raise LibraryError("분석 중에는 고칠 수 없습니다", 409)
    if not isinstance(changes, list) or not changes:
        raise LibraryError("changes 는 바꿀 것의 목록입니다")
    cfg = load_config()
    by_key = {it["key"]: it for it in doc.get("items") or []}
    beats = AM.beats_for(cfg)
    for c in changes:
        if not isinstance(c, dict) or c.get("key") not in by_key:
            raise LibraryError(f"없는 파일입니다: {(c or {}).get('key') if isinstance(c, dict) else c}")
        it = by_key[c["key"]]
        u = {} if c.get("reset") else dict(it.get("user") or {})
        if "keep" in c:
            if not isinstance(c["keep"], bool):
                raise LibraryError("keep 은 true 또는 false 입니다")
            u["keep"] = c["keep"]
        if kind == "sfx" and "type" in c:
            if c["type"] not in S.KNOWN_TYPES:
                raise LibraryError(f"모르는 효과음 종류입니다: {c['type']}")
            u["type"] = c["type"]
        if kind == "sfx" and u.get("keep") and not (u.get("type") or (it.get("auto") or {}).get("type")):
            raise LibraryError("넣으려면 종류를 정하세요")
        if kind == "meme":
            for f, lim in (("name", 40), ("meaning", 200)):
                if f in c:
                    if not isinstance(c[f], str) or len(c[f]) > lim:
                        raise LibraryError(f"{'이름' if f == 'name' else '뜻'}은 {lim}자까지입니다")
                    if c[f].strip():
                        u[f] = c[f].strip()
                    else:              # 비우면 자동으로 적은 것으로 되돌린다
                        u.pop(f, None)
            if "beats" in c:
                if not isinstance(c["beats"], list) or not c["beats"] or any(b not in beats for b in c["beats"]):
                    raise LibraryError("분위기는 정해진 것 중 하나 이상입니다")
                u["beats"] = list(dict.fromkeys(c["beats"]))[:3]
        it["user"] = u
    pack = pack_dir(kind, pid)
    src = Path(doc["source_dir"])
    if kind == "sfx":
        res = AS.build(doc, pack, src, cfg, doc.get("types") or AS.wanted_types(cfg.get("sfx") or {}))
    else:
        res = AM.build(doc, pack, src, cfg)
    _count(doc, res)
    write(doc)
    return detail(doc)


# ------------------------------------------------------------------ 설정에 넣기·빼기, 지우기, 파일

def _dirs(kind: str) -> list[str]:
    from recap_shorts import settings
    return settings.sfx_dirs() if kind == "sfx" else settings.meme_dirs()


def _save_dirs(kind: str, dirs: list[str]) -> None:
    from recap_shorts import settings
    try:
        if kind == "sfx":
            settings.save(sfx={"dirs": dirs})
        else:
            settings.save(memes={"dirs": dirs})
    except settings.SettingsError as e:
        raise LibraryError(str(e), e.status)


def use(kind: str, pid: str, on: bool) -> dict:
    """꾸러미를 설정의 폴더 목록에 넣거나 뺀다. 넣을 때 원본 폴더가 목록에 있으면 그 자리를 꾸러미로 바꾼다."""
    doc = get(kind, pid)
    me = str(pack_dir(kind, pid).resolve())
    srcp = str(Path(doc.get("source_dir") or "").resolve()) if doc.get("source_dir") else None
    dirs = [d for d in _dirs(kind) if str(Path(d).resolve()) != me]
    if on:
        if not (doc.get("counts") or {}).get("kept"):
            raise LibraryError("고른 파일이 없어 쓸 수 없습니다 — 결과에서 넣을 것을 고르세요")
        at = next((i for i, d in enumerate(dirs) if srcp and str(Path(d).resolve()) == srcp), None)
        if at is not None:
            dirs[at] = me
        else:
            dirs.append(me)
    _save_dirs(kind, dirs)
    return summary(doc)


def add(kind: str, folder: str) -> dict:
    """설정 화면의 '폴더 넣기': manifest.json 이 있으면 바로 넣고, 없으면 분석해서 끝나면 넣는다."""
    _kind(kind)
    src = resolve_dir(folder)
    if not src.is_dir():
        raise LibraryError(f"폴더가 없습니다: {_typed(folder)}")
    if (src / "manifest.json").is_file():
        from recap_shorts import settings
        dirs = _dirs(kind)
        if str(src) not in [str(Path(d).resolve()) for d in dirs]:
            _save_dirs(kind, dirs + [str(src)])
        return {"added": True, "settings": settings.public_view()}
    return {"analysis": start(kind, str(src), use=True)}


def delete(kind: str, pid: str) -> None:
    get(kind, pid)
    if _is_running(kind, pid):
        raise LibraryError("분석 중에는 지울 수 없습니다 — 먼저 멈추세요", 409)
    d = pack_dir(kind, pid)
    if _used(kind, pid):
        use(kind, pid, False)
    d.resolve().relative_to(root().resolve())    # 꾸러미 자리 밖이면 ValueError — 지우지 않는다
    shutil.rmtree(d)


def file_path(kind: str, pid: str, key: str | None = None, out: str | None = None) -> Path:
    """화면에서 틀거나 보여 줄 파일. key = 원본(분석한 목록에 있는 것만), out = 꾸러미 안에서 만든 것."""
    doc = get(kind, pid)
    if key is not None:
        if key not in {it["key"] for it in doc.get("items") or []}:
            raise LibraryError("없는 파일입니다", 404)
        p = Path(doc["source_dir"]) / key
    elif out == "_sheet" and kind == "sfx":
        p = pack_dir(kind, pid) / "_들어보기_전체.wav"
    elif out:
        try:
            man = json.loads((pack_dir(kind, pid) / "manifest.json").read_text(encoding="utf-8")).get("items", [])
        except (OSError, ValueError):
            man = []
        rec = next((r for r in man if r.get("id") == out), None)
        if rec is None:
            raise LibraryError("없는 파일입니다", 404)
        p = pack_dir(kind, pid) / str(rec["file"])
    else:
        raise LibraryError("key 나 out 중 하나를 주세요")
    if not p.is_file():
        raise LibraryError("파일이 없습니다(옮기거나 지웠을 수 있음)", 404)
    return p
