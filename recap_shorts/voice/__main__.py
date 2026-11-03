"""python -m recap_shorts.voice <script.json> <out_dir> [--sid N] [--no-asr] [--no-fit]
   [--engine qwen --instruct "..." --seed N]"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .pipeline import make_voice


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="python -m recap_shorts.voice")
    ap.add_argument("script")
    ap.add_argument("out_dir")
    ap.add_argument("--sid", type=int, default=None, help="Supertonic 화자 번호 (기본 RECAP_TTS_SID 또는 0)")
    ap.add_argument("--engine", choices=["supertonic", "qwen"], default="qwen")
    ap.add_argument("--instruct", default=None, help="기존 로컬 TTS 에 전달할 목소리 설명")
    ap.add_argument("--seed", type=int, default=None, help="qwen 합성 seed (기본 1)")
    ap.add_argument("--no-asr", action="store_true")
    ap.add_argument("--no-fit", action="store_true")
    a = ap.parse_args(argv)
    script = json.loads(Path(a.script).read_text(encoding="utf-8"))
    t = make_voice(script, a.out_dir, sid=a.sid, check_asr=not a.no_asr, fit=not a.no_fit,
                   engine=a.engine, instruct=a.instruct, seed=a.seed)
    print(f"voice.wav {t['duration']:.2f}초, 음절 {t['syllables']}, 말 속도 {t['rate']:.2f}/초 (배율 {t['tempo_factor']})")
    for w in t["warnings"] + t["flags"]:
        print("  -", w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
