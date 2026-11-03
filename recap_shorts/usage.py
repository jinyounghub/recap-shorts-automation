"""유료·한도 있는 외부 호출 기록(비용 계산용). 호출마다 JSON 한 줄을 work/_usage.jsonl 에 붙인다.

- 기록: 시각, 종류(codex / local_tts / imagegen), 모델, 토큰·뉴런·초 같은 양. 키·프롬프트 본문은 남기지 않는다.
- 경로: 환경변수 RECAP_USAGE_LOG (빈 문자열이면 끔), 기본 <저장소>/work/_usage.jsonl (work/ 는 gitignore).
- pytest 안에서는 기록하지 않는다. 기록이 실패해도 호출은 그대로 진행한다.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_lock = threading.Lock()


def _path() -> Path | None:
    if "PYTEST_CURRENT_TEST" in os.environ:
        return None
    p = os.environ.get("RECAP_USAGE_LOG")
    if p is not None:
        return Path(p) if p.strip() else None
    return ROOT / "work" / "_usage.jsonl"


def log(kind: str, **fields) -> None:
    p = _path()
    if p is None:
        return
    row = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "kind": kind, **fields}
    try:
        with _lock:
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def read(since: str | None = None, until: str | None = None, path: Path | None = None) -> list[dict]:
    """기록을 읽는다. since/until 은 ISO 시각(UTC) 문자열 비교."""
    p = path or _path() or (ROOT / "work" / "_usage.jsonl")
    if not p.is_file():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if since and r.get("ts", "") < since:
            continue
        if until and r.get("ts", "") > until:
            continue
        rows.append(r)
    return rows
