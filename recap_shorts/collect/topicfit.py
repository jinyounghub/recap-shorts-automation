"""채널 대주제 — 주제 찾기(실시간 베스트)에서 대주제에 맞는 글을 먼저 보여 준다.

- 설정: config/channel.json(커밋, 기본값·프리셋) + config/channel.local.json(gitignore, 화면에서 고른 값). local 이 있으면 그쪽이 우선.
  main_topic 이 null 이면 "없음" — 이때는 점수도 매기지 않고 LLM 도 부르지 않는다(예전 동작 그대로).
- 점수: 제목(+ 앞의 [갤러리] 표시)만 보고 2=딱 맞음 · 1=조금 · 0=아님. 안 매긴 글만 batch 개씩 Codex 한 번에 묻고(JSON),
  글×대주제마다 work/_topics/fit_<번호>_<대주제키>.json 에 보관해 다시 묻지 않는다. 대주제 설명(desc)이나 질문 문구(FIT_VERSION)가
  바뀌면 키가 달라져 다시 매긴다. 실패한 묶음은 저장하지 않아 다음에 다시 묻는다.
- 사용량 기록: llm.codex 가 호출마다 work/_usage.jsonl 에 codex 로 남긴다(여기서 따로 쓰지 않음 — 이중 집계 방지).
- 순서: 2 → 1 → (점수 없음) → 0, 같은 묶음 안에서는 원래 순서 그대로. 점수를 못 매기면 순서도 그대로.
- CLI: python -m recap_shorts.collect topics-fit --topic daily_drama --dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "config" / "channel.json"
LOCAL_PATH = ROOT / "config" / "channel.local.json"
FIT_VERSION = 1                 # 질문 문구를 바꾸면 올린다 → 보관된 점수가 달라진 키로 다시 매겨진다
DEFAULT_BATCH = 30
DEFAULT_MODEL = None  # inherit the current Codex model
MAX_CUSTOM = 200
MAX_TITLE = 120
FIT_TEXT = {2: "딱 맞음", 1: "조금", 0: "아님"}
_RANK = {2: 0, 1: 1, None: 2, 0: 3}      # 정렬 순서: 딱 맞음 → 조금 → 점수 없음 → 아님

LLM = Callable[[str, str | None], "dict | str"]   # (프롬프트, 모델) → JSON 객체


@dataclass(frozen=True)
class Topic:
    id: str         # 프리셋 id 또는 "custom"
    label: str
    desc: str
    key: str        # 보관 파일 이름에 쓰는 키: <id>-<설명 해시 8자>


# ---------------------------------------------------------------- 설정
def _read(path: Path | str | None) -> dict:
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8")) if path else {}
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _codex_model(value) -> str | None:
    """Discard model names from old Google configurations on migration."""
    model = str(value or "").strip()
    return model if model and not model.lower().startswith("gemini") else None


def load_config(path: Path | str | None = None, local: Path | str | None = None) -> dict:
    """커밋된 설정 위에 local 설정(main_topic·custom)을 덮은 결과. 파일이 없거나 깨졌으면 "없음"."""
    base = _read(path or CONFIG_PATH)
    loc = _read(local or LOCAL_PATH)
    presets = [p for p in (base.get("presets") or []) if isinstance(p, dict) and p.get("id") and p.get("label")]
    fit = base.get("fit") if isinstance(base.get("fit"), dict) else {}
    cfg = {"main_topic": base.get("main_topic"), "custom": base.get("custom"), "presets": presets,
           "fit": {"batch": int(fit.get("batch") or DEFAULT_BATCH), "model": _codex_model(fit.get("model"))},
           "source": "default"}
    if "main_topic" in loc:          # null(없음)도 값이다 — 커밋된 기본값을 끄는 선택
        cfg["main_topic"] = loc["main_topic"]
        cfg["source"] = "local"
    if "custom" in loc:
        cfg["custom"] = loc["custom"]
    return cfg


def _clean_custom(s) -> str:
    return re.sub(r"\s+", " ", s).strip() if isinstance(s, str) else ""


def _key(tid: str, desc: str) -> str:
    return f"{tid}-" + hashlib.sha1(f"v{FIT_VERSION}|{tid}|{desc}".encode("utf-8")).hexdigest()[:8]


def resolve(cfg: dict) -> Topic | None:
    """지금 정해진 대주제. 없음·모르는 id·빈 직접 입력이면 None."""
    mt = cfg.get("main_topic")
    if not mt:
        return None
    if mt == "custom":
        txt = _clean_custom(cfg.get("custom"))[:MAX_CUSTOM]
        return Topic("custom", txt[:24] + ("…" if len(txt) > 24 else ""), txt, _key("custom", txt)) if txt else None
    for p in cfg.get("presets", []):
        if p["id"] == mt:
            desc = str(p.get("desc") or p["label"])
            return Topic(str(mt), str(p["label"]), desc, _key(str(mt), desc))
    return None


def public_view(cfg: dict) -> dict:
    """GET/PUT /api/channel 응답."""
    t = resolve(cfg)
    return {"main_topic": t.id if t else None, "label": t.label if t else None,
            "custom": _clean_custom(cfg.get("custom")) or None, "source": cfg.get("source", "default"),
            "presets": [{"id": p["id"], "label": p["label"], "desc": p.get("desc", "")} for p in cfg.get("presets", [])]}


def save_local(main_topic, custom=..., *, path: Path | str | None = None, local: Path | str | None = None) -> dict:
    """화면에서 고른 대주제를 config/channel.local.json 에 저장한다. 잘못된 값이면 ValueError(한국어).
    custom 을 생략하면 이전 직접 입력 글을 그대로 둔다(다른 걸 골랐다가 돌아와도 안 사라짐)."""
    cfg = load_config(path, local)
    if main_topic in ("", "none"):
        main_topic = None
    ids = {p["id"] for p in cfg["presets"]}
    if main_topic is not None and main_topic != "custom" and main_topic not in ids:
        raise ValueError(f"모르는 대주제입니다: {main_topic}")
    if custom is ...:
        txt = _clean_custom(cfg.get("custom"))
    else:
        if custom is not None and not isinstance(custom, str):
            raise ValueError("직접 입력은 글이어야 합니다")
        txt = _clean_custom(custom)
        if len(txt) > MAX_CUSTOM:
            raise ValueError(f"직접 입력은 {MAX_CUSTOM}자까지입니다")
    if main_topic == "custom" and not txt:
        raise ValueError("직접 입력할 대주제를 적어 주세요")
    dest = Path(local or LOCAL_PATH)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_text(json.dumps({"main_topic": main_topic, "custom": txt or None}, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, dest)
    return load_config(path, local)


# ---------------------------------------------------------------- 점수 매기기
def build_prompt(topic: Topic, rows: list[tuple[int, str, str]]) -> str:
    """rows: (번호, 갤러리 표시, 제목). 번호는 이 묶음 안의 1부터."""
    lines = []
    for n, tag, title in rows:
        t = re.sub(r"\s+", " ", title or "").strip()[:MAX_TITLE]
        lines.append(f"{n}. " + (f"[{tag}] " if tag else "") + t)
    return (
        "너는 유튜브 쇼츠 채널의 주제 편집자다. 커뮤니티 인기글 제목이 채널의 대주제에 얼마나 맞는지 0~2점으로 매긴다.\n\n"
        f"채널 대주제: {topic.label}\n설명: {topic.desc}\n\n"
        "점수 기준\n"
        "2 = 딱 맞음: 대주제의 전형적인 소재라서 이 채널에 그대로 올려도 어색하지 않다.\n"
        "1 = 조금: 대주제에 걸쳐 있지만 핵심은 아니거나, 제목만으로는 맞는지 애매하다.\n"
        "0 = 아님: 대주제와 상관없는 분야다(정치 공방, 연예 가십, 경기 결과, 사진·짤 자랑처럼 설명과 다른 종류).\n\n"
        "규칙\n"
        "- 제목만 보고 판단한다. 제목 앞의 [갤러리] 표시는 글이 올라온 곳이다.\n"
        "- 재미·조회수·논쟁 정도는 보지 않는다. 대주제와 맞는지만 본다.\n"
        "- 제목에 없는 내용을 지어내지 않는다. 모든 번호에 점수를 하나씩 매긴다.\n\n"
        "글 제목\n" + "\n".join(lines) + "\n\n"
        '답은 JSON 객체 하나만, 번호를 키로: {"fits": {"1": 2, "2": 0}}'
    )


def parse_answer(ans, n: int) -> dict[int, int]:
    """모델 답 → {묶음 안 번호: 0|1|2}. 모양이 틀린 항목은 버린다(그 글은 점수 없음으로 남아 다음에 다시 묻는다)."""
    raw = ans.get("fits", ans.get("scores", ans)) if isinstance(ans, dict) else ans
    if isinstance(raw, dict):
        pairs = list(raw.items())
    elif isinstance(raw, list):
        pairs = [(r.get("i", r.get("n", r.get("id"))), r.get("fit", r.get("score"))) for r in raw if isinstance(r, dict)]
    else:
        return {}
    out: dict[int, int] = {}
    for k, v in pairs:
        if isinstance(v, bool):
            continue
        try:
            i, f = int(k), int(v)
        except (TypeError, ValueError):
            continue
        if 1 <= i <= n and f in (0, 1, 2) and float(v) == f:
            out[i] = f
    return out


def _cache_file(cache_dir: Path, no, topic: Topic) -> Path:
    return cache_dir / f"fit_{no}_{topic.key}.json"


def _read_cache(cache_dir: Path | None, no, topic: Topic) -> int | None:
    if cache_dir is None:
        return None
    try:
        f = json.loads(_cache_file(cache_dir, no, topic).read_text(encoding="utf-8")).get("fit")
    except (OSError, ValueError, AttributeError):
        return None
    return f if f in (0, 1, 2) else None


def score(rows: list[tuple[int, str, str]], topic: Topic, llm: LLM, cache_dir: Path | None, *,
          batch: int = DEFAULT_BATCH, model: str | None = DEFAULT_MODEL) -> tuple[dict[int, int], str, str]:
    """rows: (글번호, 갤러리 표시, 제목) → ({글번호: 0|1|2}, 상태, 설명). 상태: ok | partial | unavailable.

    - 이미 매긴 글(cache_dir/fit_<번호>_<키>.json)은 다시 묻지 않는다. cache_dir 이 None 이면 보관하지 않는다(--dry-run).
    - 안 매긴 글만 batch 개씩 llm(프롬프트, 모델) 에 한 번에 넣는다. 호출이 예외를 내거나 한 묶음의 답이 하나도 못 쓰게 오면
      거기서 멈춘다 → 그때까지 매긴 것만 돌려주고, 나머지는 점수 없음(목록 순서 그대로).
    """
    got: dict[int, int] = {}
    todo: list[tuple[int, str, str]] = []
    for no, tag, title in rows:
        f = _read_cache(cache_dir, no, topic)
        if f is None:
            todo.append((no, tag, title))
        else:
            got[no] = f
    if not todo:
        return got, "ok", ""
    batch = max(1, int(batch))
    why = ""
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        prompt = build_prompt(topic, [(n, tag, title) for n, (_no, tag, title) in enumerate(chunk, 1)])
        try:
            ans = llm(prompt, model)
        except Exception as e:  # noqa: BLE001  키 없음·네트워크·하루 한도 — 더 묻지 않는다
            from recap_shorts import errors
            why = errors.reason(e)                 # 화면에 가는 문구 — 예외 이름·원문은 로그에도 남기지 않는다
            break
        ok = parse_answer(ans, len(chunk))
        if not ok:
            why = "모델 답의 모양이 맞지 않음"
            break
        for n, (no, _tag, _title) in enumerate(chunk, 1):
            if n in ok:
                got[no] = ok[n]
                if cache_dir is not None:
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    _cache_file(cache_dir, no, topic).write_text(
                        json.dumps({"fit": ok[n], "model": model, "topic": topic.id}, ensure_ascii=False), encoding="utf-8")
    missing = sum(1 for no, _t, _ti in rows if no not in got)
    if not missing:
        return got, "ok", ""
    state = "partial" if got else "unavailable"
    return got, state, (f"'{topic.label}' 맞춤 점수를 " + ("일부만 매겼습니다" if got else "매기지 못했습니다")
                        + f"({why or '일부 글의 점수가 빠짐'}) — 점수 없는 글은 원래 순서로 보여 줍니다")


def order(items: list[dict], hide_unfit: bool = False) -> list[dict]:
    """딱 맞음 → 조금 → 점수 없음 → 아님. 같은 묶음 안에서는 원래 순서(정렬이 안정적). hide_unfit 이면 '아님'은 뺀다."""
    out = sorted(items, key=lambda it: _RANK[it.get("fit")])
    return [it for it in out if it.get("fit") != 0] if hide_unfit else out


def apply_to_views(items: list[dict], topic: Topic | None, llm: LLM, cache_dir: Path | None, *, batch: int = DEFAULT_BATCH,
                   model: str | None = DEFAULT_MODEL, hide_unfit: bool = False) -> tuple[list[dict], dict]:
    """topic_view 목록에 fit 을 붙이고 순서를 바꾼다 → (목록, 응답에 덧붙일 필드). 대주제가 없으면 아무것도 안 한다(LLM 호출 0)."""
    if topic is None:
        return items, {"channel": None, "fit": "off", "fit_note": "", "unfit_count": 0}
    fits, state, note = score([(i["no"], i.get("tag", ""), i["title"]) for i in items], topic, llm, cache_dir,
                              batch=batch, model=model)
    for i in items:
        if i["no"] in fits:
            i["fit"] = fits[i["no"]]
    extra = {"channel": {"main_topic": topic.id, "label": topic.label}, "fit": state, "fit_note": note,
             "unfit_count": sum(1 for i in items if i.get("fit") == 0)}
    return order(items, hide_unfit), extra


# ---------------------------------------------------------------- 진짜 Codex 호출(시험 중에는 막음)
def codex_llm(prompt: str, model: str | None = DEFAULT_MODEL) -> dict:
    if os.environ.get("PYTEST_CURRENT_TEST"):
        raise RuntimeError("시험 중에는 Codex 를 부르지 않음")
    from recap_shorts.llm.codex import generate_text
    return generate_text(prompt, _codex_model(model), json=True, timeout=180)


# ---------------------------------------------------------------- CLI: python -m recap_shorts.collect topics-fit
def cli_main(argv=None, *, fetch=None, llm: LLM | None = None) -> int:
    """지금 실시간 베스트 목록에 점수를 매겨 한 번 찍어 본다. fetch(pages, hours) / llm 은 시험용 대체."""
    ap = argparse.ArgumentParser(prog="python -m recap_shorts.collect topics-fit",
                                 description="실시간 베스트 목록을 대주제에 맞춰 점수 매겨 찍는다(Codex 호출 묶음당 1회).")
    ap.add_argument("--topic", help="프리셋 id(예: daily_drama). 생략하면 지금 설정된 대주제")
    ap.add_argument("--custom", help="직접 입력한 대주제 문장(--topic 대신)")
    ap.add_argument("--pages", type=int, help="읽을 쪽 수(기본: config/topics.json)")
    ap.add_argument("--hours", type=int, help="최근 몇 시간(기본: config/topics.json)")
    ap.add_argument("--limit", type=int, default=0, help="점수 매길 글 수 상한(조회수 많은 순, 0=전부)")
    ap.add_argument("--dry-run", action="store_true", help="점수를 work/_topics 에 보관하지 않고 찍기만 한다")
    a = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    cfg = load_config()
    if a.custom:
        cfg["main_topic"], cfg["custom"] = "custom", a.custom
    elif a.topic:
        cfg["main_topic"] = a.topic
    topic = resolve(cfg)
    if topic is None:
        print("대주제가 없습니다. --topic 에 프리셋 id 를 주세요: " + ", ".join(p["id"] for p in cfg["presets"]) + " 또는 --custom \"문장\"")
        return 2

    from . import dcbest
    d = dcbest.load_config().get("defaults", {})
    pages, hours = a.pages or int(d.get("pages", 3)), a.hours or int(d.get("hours", 48))
    if fetch is None:
        from .http import HttpClient

        def fetch(pg, hr):
            return dcbest.fetch_best(pg, hr, "views", HttpClient(), max_pages=int(d.get("max_pages", 5)))
    items = fetch(pages, hours)
    if a.limit > 0:
        items = items[:a.limit]
    print(f"대주제: {topic.label} — {topic.desc}")
    print(f"목록 {len(items)}개 (최근 {hours}시간, {pages}쪽)\n")

    calls = []
    base = llm or codex_llm

    def counting(prompt, model):
        calls.append(len(prompt))
        return base(prompt, model)

    cache_dir = None if a.dry_run else ROOT / "work" / "_topics"
    t0 = datetime.now(timezone.utc).isoformat(timespec="seconds")
    s0 = time.time()
    fits, state, note = score([(i.no, i.tag, i.title) for i in items], topic, counting, cache_dir,
                              batch=cfg["fit"]["batch"], model=cfg["fit"]["model"])
    views = [{"no": i.no, "tag": i.tag, "title": i.title, "views": i.views, "fit": fits.get(i.no)} for i in items]
    for v in order(views):
        mark = "-" if v["fit"] is None else str(v["fit"])
        print(f"{mark}  {v['views']:>7,}  " + (f"[{v['tag']}] " if v["tag"] else "") + v["title"])
    cnt = {k: sum(1 for v in views if v["fit"] == k) for k in (2, 1, 0)}
    print(f"\n점수: 딱 맞음(2) {cnt[2]} · 조금(1) {cnt[1]} · 아님(0) {cnt[0]} · 없음(-) {sum(1 for v in views if v['fit'] is None)}"
          f"  / 상태 {state}" + (f"  / {note}" if note else ""))
    toks = [r for r in _usage_since(t0) if "prompt_tokens" in r]
    print(f"Codex 호출 {len(calls)}회, {time.time() - s0:.1f}초"
          + (f", 토큰 입력 {sum(r.get('prompt_tokens') or 0 for r in toks)} · 출력 {sum(r.get('output_tokens') or 0 for r in toks)}"
             f" · 생각 {sum(r.get('thought_tokens') or 0 for r in toks)} (work/_usage.jsonl 기록)" if toks else "")
          + (" — 보관 안 함(--dry-run)" if a.dry_run else f" — 점수를 {cache_dir} 에 보관"))
    return 0 if state != "unavailable" else 1


def _usage_since(ts: str) -> list[dict]:
    try:
        from recap_shorts import usage
        return [r for r in usage.read(since=ts) if r.get("kind") == "codex"]
    except Exception:  # noqa: BLE001
        return []
