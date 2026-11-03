"""python -m recap_shorts.script <topic.json> <out_dir> [--model M] [--max-calls N]"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from recap_shorts.llm.codex import CodexLLM
from .pipeline import generate_script


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="python -m recap_shorts.script")
    ap.add_argument("topic")
    ap.add_argument("out_dir")
    ap.add_argument("--model", default=None, help="생략하면 현재 Codex 모델 설정을 사용")
    ap.add_argument("--max-calls", type=int, default=2, help="Codex 최대 호출 수")
    a = ap.parse_args(argv)
    topic = json.loads(Path(a.topic).read_text(encoding="utf-8"))
    llm = CodexLLM(a.model, max_calls=a.max_calls)
    script, check = generate_script(topic, a.out_dir, llm)
    print(f"script.json 저장: {a.out_dir}")
    print(f"음절 {check['checks']['syllables']['total']}, 삭제 {len(check['deleted'])}문장, flags {len(check['flags'])}")
    for f in check["flags"]:
        print("  -", f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
