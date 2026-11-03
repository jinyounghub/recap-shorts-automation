"""타임라인에 짤 오버레이·전체화면 짤 샷·효과음을 얹는다 (beats → memes → sfx)."""
from __future__ import annotations

import copy
import json
from pathlib import Path

from . import beats as B
from . import meme_fit as MF
from . import memes as M
from . import intro as I
from . import sfx as S
from .. import wordcuts as WC
from .config import ROOT, enabled, load_config, resolve_path


SFX_DROP = "fx/sfx_drop.json"   # 작업마다 사람이 뺀 효과음 [{"t": 15.785, "id": "scream_01"}] — 다시 렌더해도 유지
BUILTIN_LIBRARY = "assets/sfx"  # 저장소의 합성 효과음(scripts/make_sfx.py). 기본 꾸러미(sfx.libraries)가 없을 때 대신 씀


def choose_sfx_library(scfg: dict) -> tuple[list[dict], list[str], bool]:
    """설정 화면에서 고른 효과음 폴더 → (없거나 비면) config 의 sfx.libraries(기본 꾸러미).
    고른 폴더에 없는 종류는 fill_missing(기본 켬)이면 기본 꾸러미에서 채운다. (항목, 메모, 고른 폴더를 썼는지)."""
    from recap_shorts import settings
    default_dirs = scfg.get("libraries", [BUILTIN_LIBRARY])
    custom = settings.sfx_dirs()
    if not custom:
        lib, notes = S.load_library(default_dirs)
        return lib, notes, False
    lib, _ = S.load_library(custom)
    if not lib:
        lib, notes = S.load_library(default_dirs)
        return lib, ["설정한 효과음 폴더에서 효과음을 찾지 못해 기본 효과음을 씀"] + notes, False
    notes = [f"설정한 효과음 폴더를 씀({len(custom)}개 폴더, 효과음 {len(lib)}개)"]
    if settings.sfx_fill_missing():
        have = {it["type"] for it in lib}
        wanted = S.slot_types(scfg)     # 규칙이 쓰는 종류만 채운다(안 쓰는 종류의 기본 소리가 후보에 끼지 않게)
        base, _ = S.load_library(default_dirs)
        extra = [{**it, "priority": int(it.get("priority", 0)) + len(custom)} for it in base
                 if it["type"] not in have and (not wanted or it["type"] in wanted)]
        if extra:
            lib = lib + extra
            notes.append("폴더에 없는 종류는 기본 효과음으로 채움: " + ", ".join(sorted({it["type"] for it in extra})))
    return lib, notes, True


def choose_meme_library(mcfg: dict) -> tuple[list[dict], str]:
    """설정 화면에서 고른 짤 폴더 → (없거나 비면) config 의 memes.library. (항목, 메모)."""
    from recap_shorts import settings
    dirs = settings.meme_dirs()
    default = resolve_path(mcfg.get("library", "assets_local/memes"))
    if not dirs:
        return M.load_library(default)
    lib, notes = M.load_libraries(dirs)
    if lib:
        return lib, f"설정한 짤 폴더를 씀({len(dirs)}개 폴더, 짤 {len(lib)}장)"
    lib, note = M.load_library(default)
    return lib, " · ".join(["설정한 짤 폴더에서 짤을 찾지 못해 기본 짤을 씀"] + notes + ([note] if note else []))


def sfx_drops(job_dir: str | Path) -> list[dict]:
    p = Path(job_dir) / SFX_DROP
    try:
        v = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else []
    except (OSError, ValueError):
        return []
    return [d for d in v if isinstance(d, dict) and isinstance(d.get("t"), (int, float))] if isinstance(v, list) else []


def dropped(ev: dict, d: dict, tol: float = 0.3) -> bool:
    """효과음 ev 가 뺄 목록의 d 와 같은가: 시각이 tol 초 안이고, d 에 id 가 있으면 id 도 같을 것."""
    return abs(float(ev.get("t", -99)) - float(d["t"])) <= tol and (not d.get("id") or d["id"] == ev.get("id"))


def sentences_with_beats(timings: dict, beat_result: dict) -> list[dict]:
    sents = timings["sentences"]
    return [{"index": s["index"], "role": s.get("role"), "text": s["text"], "start": float(s["start"]),
             "end": float(s["end"]), "beat": beat_result["beats"][i]} for i, s in enumerate(sents)]


CORNERS = {"box_br": (1.0, 1.0), "box_bl": (0.0, 1.0), "box_tr": (1.0, 0.0), "box_tl": (0.0, 0.0), "box_center": (0.5, 0.5)}


def away_from_focus(shots: list[dict], ev: dict, anchors: list[str]) -> str:
    """스티커가 그 순간 화면의 중심 대상(샷의 focus)을 가리지 않게, 허용된 자리 중 focus 에서 가장 먼 구석.
    구석 자리가 정해져 있으면 그림 속 중요한 숫자·글자를 스티커가 덮을 수 있다."""
    shot = next((s for s in shots if s["start"] <= ev["start"] < s["end"]), None)
    fx, fy = (shot or {}).get("focus") or (0.5, 0.5)
    ok = [a for a in anchors if a in CORNERS] or ["box_br"]
    return max(ok, key=lambda a: ((CORNERS[a][0] - fx) ** 2 + (CORNERS[a][1] - fy) ** 2, -ok.index(a)))


def _drop_rank(shot: dict) -> int:
    """줄일 때 먼저 뺄 컷: AI 그림(보조) → 잘라 낸 캡처 → 통째 캡처."""
    f = str(shot.get("file", "")).replace("\\", "/")
    return 0 if "assets/ai/" in f else (1 if "_s" in Path(f).stem else 2)


def fit_before_meme(shots: list[dict], sents: list[dict], meme: dict, min_len: float = 0.9, word_cuts=None) -> list[dict]:
    """문장 끝에 붙는 짤 샷을 넣는다. 그 문장의 원래 컷들을 '문장 시작 ~ 짤 시작'에 다시 나누고, 한 컷이
    min_len 보다 짧아지면 AI 컷부터 뺀다. 짤이 문장 끝 컷을 덮으면 그 장면이 아주 짧게만 보일 수 있다.
    word_cuts(wordcuts.WordCuts)가 있으면 timeline.build 와 같은 규칙으로 컷을 어절 경계에 놓는다(못 고르면 고르게 나눔).
    그 무리 안에 장면 편집기에서 사람이 옮긴 샷(cut="user")이 있으면 다시 나누지도 빼지도 않고 insert_shot 으로 짤만 끼운다.
    같은 문장 안 그림 사이 경계를 끌어 샷 길이를 바꾼 값이 저장되면 짤 단계가 덮어쓰면 안 된다."""
    sent_starts = sorted(float(s["start"]) for s in sents if s.get("role") != "hook")
    lo = max([t for t in sent_starts if t <= meme["start"] + 1e-6], default=0.0)
    lo = min((s["start"] for s in shots if s["start"] >= lo - 0.05 and s["start"] < meme["start"]), default=lo)
    group = [s for s in shots if s.get("kind") != "meme" and s["start"] >= lo - 1e-6 and s["start"] < meme["end"] - 1e-6]
    if not group:
        return M.insert_shot(shots, meme)
    if any(s.get("cut") == "user" for s in group):
        return M.insert_shot(shots, meme)     # 사람이 옮긴 경계는 그대로 두고 짤만 끼운다(겹치는 샷만 잘림)
    rest = [s for s in shots if s not in group]
    span = meme["start"] - lo
    keep = list(group)
    while len(keep) > 1 and span / len(keep) < min_len:
        victim = min(reversed(keep), key=_drop_rank)       # 같은 순위면 뒤쪽 컷부터
        keep.remove(victim)
    step = span / len(keep) if keep else 0.0
    picks = None
    if word_cuts is not None and len(keep) > 1:
        owner = max((s for s in sents if s.get("role") != "hook" and float(s["start"]) <= lo + 0.06),
                    key=lambda s: float(s["start"]), default=None)
        if owner is not None:
            picks = word_cuts.choose(owner["index"], lo, float(meme["start"]), len(keep))
    edges = [lo] + [c.t for c in picks] + [meme["start"]] if picks else [lo + k * step for k in range(len(keep) + 1)]
    new = [{**s, "start": round(edges[k], 3), "end": round(edges[k + 1], 3)} for k, s in enumerate(keep)]
    tail = [s for s in rest if s["start"] >= meme["end"] - 1e-6 or s["end"] <= lo + 1e-6]
    # 짤 뒤로 이어지는 컷이 짤과 겹치면(마지막 문장이 아닐 때 다음 문장 첫 컷) 짤 끝에서 시작하게 맞춘다
    out = sorted(tail + new + [meme], key=lambda s: s["start"])
    return out


def snap_meme_start(word_cuts, ev: dict, sents: list[dict], mcfg: dict) -> float | None:
    """문장 끝 짤(전체화면 샷)의 시작도 그림이 바뀌는 자리이므로 가까운 어절 경계로 옮긴다(± word_cuts.snap_meme_s).
    ev["start"] 를 고치고 원래 값을 돌려준다(못 옮기면 None). 짤 길이는 full_shot_min_s 이상, 앞에 남는 구간은 0.7초 이상."""
    owner = next((s for s in sents if s["index"] == ev.get("sentence")), None)
    if owner is None or owner.get("role") == "hook":
        return None
    lo = float(owner["start"]) + 0.7
    hi = float(ev["end"]) - float(mcfg.get("full_shot_min_s", 0.6))
    cut = word_cuts.snap_meme(owner["index"], float(ev["start"]), lo, hi)
    if cut is None or abs(cut.t - float(ev["start"])) < 1e-6:
        return None
    old = float(ev["start"])
    ev["start"] = round(cut.t, 3)
    return old


def _copy_into_job(ev: dict, job_dir: Path) -> None:
    """짤 그림을 작업 폴더 assets/meme/ 에 복사하고 ev["file"] 을 그 상대 경로로 바꾼다. 원래 자리는 meme_src 에.
    앱은 작업 폴더 안 파일만 보여 주므로 짤이 assets_local 절대 경로에 있으면 장면 편집기에서 보이지 않는다."""
    import hashlib
    import shutil
    src = Path(ev["file"])
    if not src.is_file():
        return
    # 이름 앞에 원래 자리의 해시: 꾸러미마다 m_001.png 가 있고 한 폴더 안에도 01.png·sub/01.png 가 있을 수 있어
    # 파일 이름만 쓰면 서로 덮어써 다른 그림이 나왔다
    name = f"{hashlib.sha1(str(src.resolve()).lower().encode('utf-8')).hexdigest()[:8]}_{src.name}"
    dst = job_dir / "assets" / "meme" / name
    dst.parent.mkdir(parents=True, exist_ok=True)
    s = src.stat()
    if not dst.is_file() or (dst.stat().st_size, dst.stat().st_mtime_ns) != (s.st_size, s.st_mtime_ns):
        shutil.copy2(src, dst)
    try:
        ev["meme_src"] = src.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        ev["meme_src"] = str(src)
    ev["file"] = f"assets/meme/{name}"


def apply_fx(timeline: dict, timings: dict, job_dir: str | Path, cfg: dict, *, memes: bool | None = None,
             sfx: bool | None = None, use_llm: bool | None = None, llm=None) -> tuple[dict, dict]:
    """(새 타임라인, 보고). 원래 timeline 은 건드리지 않는다. memes/sfx 가 None 이면 설정의 enabled 를 따른다."""
    job_dir = Path(job_dir)
    tl = copy.deepcopy(timeline)
    tl.pop("intro", None)
    do_memes = bool(cfg.get("memes", {}).get("enabled")) if memes is None else memes
    do_sfx = bool(cfg.get("sfx", {}).get("enabled")) if sfx is None else sfx
    report: dict = {"memes": do_memes, "sfx": do_sfx, "notes": []}
    if not (do_memes or do_sfx):
        return tl, report

    # 그림 컷을 어절 경계에 놓는 규칙(timeline.build 와 같은 것) — fit_before_meme·짤 시작에도 쓴다. 꺼져 있으면 None
    lead_cfg = float((cfg.get("timing") or {}).get("picture_lead_s", 0.0))
    wc = WC.for_job(job_dir, timings, cfg, delay_s=float((timeline.get("audio") or {}).get("voice_offset_s") or lead_cfg),
                    voice=(timeline.get("audio") or {}).get("voice") or "voice/voice.wav") if do_memes else None

    bcfg = cfg.get("beats", {})
    texts = [s["text"] for s in timings["sentences"]]
    br = B.label_beats(texts, cache_path=job_dir / bcfg.get("cache", "fx/beats.json"),
                       use_llm=bcfg.get("use_llm", True) if use_llm is None else use_llm,
                       model=bcfg.get("model"), llm=llm)
    report["beats"] = {"source": br["source"], "labels": br["beats"], "error": br.get("error")}
    sents = sentences_with_beats(timings, br)

    mcfg = cfg.get("memes", {})
    overlays: list[dict] = list(tl.get("overlays") or [])
    meme_events: list[dict] = []
    if do_memes:
        lib, note = choose_meme_library(mcfg)
        if note:
            report["notes"].append(note)
        fits = None
        if lib and mcfg.get("pick_by") == "fit":
            # 박자 라벨만으로는 상황에 안 맞는 짤이 붙는다 — 박자 라벨 대신 문장과 짤 뜻을 함께 보고 고른다(fx/meme_fit.py)
            dur0 = float(timeline["duration"])
            fr = MF.fit_memes(sents, lib, M.eligible_indices(sents, mcfg, dur0), M.effective_max(lib, mcfg, dur0),
                              cache_path=job_dir / mcfg.get("fit_cache", "fx/meme_fit.json"),
                              use_llm=bcfg.get("use_llm", True) if use_llm is None else use_llm,
                              model=mcfg.get("fit_model", bcfg.get("model")), llm=llm)
            fits = fr["picks"]
            report["meme_fit"] = {"source": fr["source"], "picks": fits, "error": fr.get("error")}
            if fr.get("error"):
                report["notes"].append(f"짤 고르기 실패({fr['error'][:80]}) — 짤 없이 감")
        picked = M.pick_memes(sents, lib, mcfg, seed=job_dir.name, duration=float(timeline["duration"]), fits=fits)
        report["notes"] += picked["notes"]
        meme_events = picked["events"]
        for ev in meme_events:
            _copy_into_job(ev, job_dir)
        for ev in meme_events:
            if ev["mode"] == "shot" and mcfg.get("position", "end") == "end":
                if wc is not None and snap_meme_start(wc, ev, sents, mcfg) is not None:
                    report["notes"].append(f"짤 시작을 어절 경계로 옮김: 문장 {ev['sentence']} → {ev['start']:.2f}초")
                tl["shots"] = fit_before_meme(tl["shots"], sents, M.to_shot(ev, mcfg),
                                              float(mcfg.get("min_shot_s", 0.9)), word_cuts=wc)
            elif ev["mode"] == "shot":
                tl["shots"] = M.insert_shot(tl["shots"], M.to_shot(ev, mcfg))
            else:
                ev["anchor"] = away_from_focus(tl["shots"], ev, mcfg.get("sticker", {}).get("anchors") or [ev["anchor"]])
                overlays.append(M.to_overlay(ev, mcfg))
        overlays.sort(key=lambda o: o["start"])
        report["memes_placed"] = [{k: ev[k] for k in ("sentence", "beat", "meme_id", "mode", "start", "end")}
                                  for ev in meme_events]
    tl["overlays"] = overlays

    audio = tl.setdefault("audio", {})
    if do_sfx:
        scfg = cfg.get("sfx", {})
        lib, notes, _ = choose_sfx_library(scfg)
        place_cfg = scfg
        # 기본 꾸러미가 없는 PC — 설정의 fallback_libraries(예전 그대로) → 저장소의 합성음(assets/sfx)
        # 순서로 찾고, 하나도 없으면 효과음은 건너뛴다. 합성 꾸러미에는 기본 꾸러미의 자리 이름 몇 가지
        # (hit, sparkle, 밈 반응 소리)가 없어, 그때만 종류 대응표·크기를 합성 종류에 맞춘다(꾸러미가 있으면 그대로).
        if not lib:
            tried = []
            for cand in ([list(scfg["fallback_libraries"])] if scfg.get("fallback_libraries") else []) + [[BUILTIN_LIBRARY]]:
                if cand in tried:
                    continue
                tried.append(cand)
                lib, fb_notes = S.load_library(cand)
                if lib:
                    builtin = cand == [BUILTIN_LIBRARY]
                    notes = [f"설정한 효과음 꾸러미가 없어 {'저장소 합성음' if builtin else '대체 꾸러미'}"
                             f"({', '.join(cand)})을 씀"]
                    if builtin:   # 저장소 합성음으로 대신할 때만 종류 대응표·크기를 맞춘다
                        tdb, cuts = S.synth_cast(scfg.get("types_db"), scfg.get("cuts"))
                        place_cfg = {**scfg, "cuts": cuts, "types_db": tdb,
                                     "type_fallback": S.builtin_type_fallback(scfg.get("type_fallback"))}
                    break
                notes += fb_notes
        report["notes"] += notes
        # 전체화면으로 바뀐 샷 시작은 그대로 meme_events 로 효과음이 붙는다
        lead0 = float((cfg.get("timing") or {}).get("picture_lead_s", 0.0))
        energy = None
        if scfg.get("place_on") == "speech_gap" and audio.get("voice"):
            vp = Path(audio["voice"])
            energy = S.voice_energy(vp if vp.is_absolute() else job_dir / vp)
        # 카드 튐 효과음(hook.pop)이 t_pop 을 쓰므로 인트로를 먼저 만든다(목소리 지연은 아래에서 붙을 값 lead0)
        intro0 = I.build_intro(tl, timings, cfg, job_dir=job_dir,
                               voice_offset_s=float(audio.get("voice_offset_s") or lead0))
        if intro0:
            tl["intro"] = intro0
        placed = S.place_sfx(tl, sents, meme_events, lib, place_cfg, energy=energy, voice_offset_s=lead0)
        report["notes"] += placed["notes"]
        drops = sfx_drops(job_dir)
        kept = [e for e in placed["events"] if not any(dropped(e, d) for d in drops)]
        if len(kept) < len(placed["events"]):
            report["notes"].append(f"사람이 뺀 효과음 {len(placed['events']) - len(kept)}개")
        placed["events"] = kept
        audio["sfx"] = placed["events"]
        report["sfx_placed"] = len(placed["events"])
    lead = float((cfg.get("timing") or {}).get("picture_lead_s", 0.0))
    if lead > 0 and not audio.get("voice_offset_s"):
        # 그림·자막이 말보다 lead 초 먼저 바뀐다: 샷·자막은 그대로 두고 목소리를 늦추며 영상을 그만큼 늘린다
        audio["voice_offset_s"] = lead
        tl["duration"] = round(float(tl["duration"]) + lead, 3)
        if tl["shots"]:
            tl["shots"][-1]["end"] = max(tl["shots"][-1]["end"], tl["duration"])
        report["picture_lead_s"] = lead
    # 인트로 카드: 목소리 지연이 정해진 뒤에 말 시각으로 t_pop 을 잡는다
    intro = tl.get("intro") or I.build_intro(tl, timings, cfg, job_dir=job_dir, voice_offset_s=float(audio.get("voice_offset_s") or 0.0))
    if intro:
        tl["intro"] = intro
        report["intro"] = {k: intro[k] for k in ("label", "lines", "t_pop", "t_cut")}
    tl["fx"] = {"beats": br["beats"], "beats_source": br["source"], "notes": report["notes"]}
    return tl, report


def _opt_flags(options: dict | None) -> dict:
    """작업 옵션으로 켠 경우, 설정의 개별 enabled 가 꺼져 있어도 짤·효과음을 모두 켠다(옵션 fx 에서 따로 끈 것만 끔)."""
    override = (options or {}).get("fx")
    flag = {"memes": True, "sfx": True}
    for k in ("memes", "sfx"):
        if isinstance(override, dict) and k in override and isinstance(override[k], dict) and "enabled" in override[k]:
            flag[k] = bool(override[k]["enabled"])
    return flag


def picture_lead(cfg: dict, options: dict | None = None) -> float:
    """maybe_apply 가 실제로 목소리를 늦출 초(그림·자막이 말보다 먼저 바뀌는 시간). fx 가 꺼졌거나 짤·효과음이 둘 다 꺼지면 0.
    timeline.build 가 문장 안 컷을 어절 경계에 놓을 때, 목소리가 늦게 나오는 만큼 컷도 늦춰 영상에서 어절 사이에 떨어지게 한다."""
    if not enabled(cfg, options):
        return 0.0
    flag = _opt_flags(options)
    if not (flag["memes"] or flag["sfx"]):
        return 0.0
    return max(0.0, float((cfg.get("timing") or {}).get("picture_lead_s", 0.0)))


def maybe_apply(job_dir: str | Path, timeline: dict, timings: dict, options: dict | None = None,
                config_path: str | Path | None = None) -> dict:
    """pipeline.write_timeline 용. 꺼져 있으면 timeline 을 그대로 돌려준다. 어떤 오류도 파이프라인을 멈추지 않는다."""
    override = (options or {}).get("fx")
    cfg = load_config(config_path, override if isinstance(override, dict) else None)
    if (options or {}).get("check_asr") is False:
        cfg.setdefault("timing", {}).setdefault("word_cuts", {})["asr"] = "off"
    if not enabled(cfg, options):
        return with_intro(timeline, timings, cfg, job_dir)
    flag = _opt_flags(options)
    try:
        tl, _ = apply_fx(timeline, timings, job_dir, cfg, memes=flag["memes"], sfx=flag["sfx"])
        return tl
    except Exception as e:  # noqa: BLE001
        out = with_intro(timeline, timings, cfg, job_dir)
        out["fx"] = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
        return out


def with_intro(timeline: dict, timings: dict, cfg: dict, job_dir: str | Path) -> dict:
    """짤·효과음을 끄거나 그 단계가 실패해도 인트로 카드는 붙인다.
    인트로는 짤·효과음 켜기/끄기와 따로 간다(끄려면 config intro.enabled)."""
    out = copy.deepcopy(timeline)
    try:
        intro = I.build_intro(out, timings, cfg, job_dir=Path(job_dir),
                              voice_offset_s=float((out.get("audio") or {}).get("voice_offset_s") or 0.0))
    except Exception:  # noqa: BLE001
        intro = None
    if intro:
        out["intro"] = intro
    return out
