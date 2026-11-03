"""python -m recap_shorts.fx <job_dir> [--memes/--no-memes] [--sfx/--no-sfx] [--render] [--out name.mp4]

기존 작업의 timeline.json 에 짤·효과음을 얹어 timeline_fx.json 으로 쓴다(timeline.json 은 건드리지 않음).

python -m recap_shorts.fx rights [--job 작업폴더] [--json]
효과음 꾸러미의 저작권 위험도 표(또는 한 작업이 쓴 주의 소리). 기록은 config/sfx_rights.json — fx/rights.py.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .apply import apply_fx
from .config import load_config


def main(argv=None) -> None:
    args = sys.argv[1:] if argv is None else list(argv)
    if args and args[0] == "rights":
        from .rights import main as rights_main
        return rights_main(args[1:])
    ap = argparse.ArgumentParser(prog="python -m recap_shorts.fx")
    ap.add_argument("job_dir")
    ap.add_argument("--memes", action=argparse.BooleanOptionalAction, default=None, help="짤 켜기/끄기 (기본: 설정 따름)")
    ap.add_argument("--sfx", action=argparse.BooleanOptionalAction, default=None, help="효과음 켜기/끄기 (기본: 설정 따름)")
    ap.add_argument("--config", default=None, help="설정 파일(기본 config/fx.json)")
    ap.add_argument("--no-llm", action="store_true", help="분위기 라벨을 규칙으로만 (Codex 호출 없음)")
    ap.add_argument("--render", action="store_true")
    ap.add_argument("--out", default="timeline_fx.mp4", help="out/ 아래 파일 이름")
    a = ap.parse_args(argv)
    job = Path(a.job_dir).resolve()
    cfg = load_config(a.config)
    tl = json.loads((job / "timeline.json").read_text(encoding="utf-8"))
    timings = json.loads((job / "voice" / "timings.json").read_text(encoding="utf-8"))
    new, rep = apply_fx(tl, timings, job, cfg, memes=a.memes, sfx=a.sfx, use_llm=False if a.no_llm else None)
    (job / "timeline_fx.json").write_text(json.dumps(new, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({**rep, "overlays": len(new.get("overlays", [])),
                      "sfx_events": len((new.get("audio") or {}).get("sfx", []))}, ensure_ascii=False, indent=1))
    if a.render:
        from recap_shorts.render.render import render
        out = job / "out" / a.out
        res = render(str(job / "timeline_fx.json"), str(out), quiet=True)
        print(json.dumps(res, ensure_ascii=False))


if __name__ == "__main__":
    main()
