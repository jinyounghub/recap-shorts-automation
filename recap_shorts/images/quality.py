"""AI 이미지 품질 검사: 한 영상의 AI 이미지를 한 번에 Codex 비전에 보여 주고 다시 만들 것을 고른다.

0~2점 매기기는 모든 장에 1점을 주는 식으로 뭉개져서, 눈에 보이는 결함을 예/아니오로 묻고
가장 나쁜 장을 순서대로 고르게 한다. 요청 1번(이미지 n장 + 지시문). 실패하면 빈 결과를 돌려주고 파이프라인은 그대로 간다.
"""
from __future__ import annotations

from pathlib import Path

INSTRUCTION = """\
위 이미지 {n}장은 한국어 이슈 요약 쇼츠의 문장별 보조 화면(AI 생성 사진)이다. 순서대로 1~{n}번.
각 번호가 보여 줘야 하는 장면 설명:
{hints}

장마다 아래 결함이 '눈에 띄게' 있으면 true, 아니면 false. 애매하면 false.
- sunset: 노을·골든아워 빛
- filler: 화분·찻잔·쿠션 같은 분위기 채움 소품이 화면의 큰 부분
- blank_screen: 아무것도 없이 빛나는 빈 화면(폰·모니터)이 주인공
- neon_float: 네온 빛, 공중에 뜬 말풍선·아이콘 같은 비현실 그래픽
- fake_text: 깨진 가짜 글자·로고가 보임
- artifacts: 손가락·얼굴·사물이 뭉개지거나 녹은 듯한 AI 티
- subject_missing: 장면 설명의 주인공(구체적인 대상)이 안 보임
- western: 한국 이야기인데 서양식 거리·실내로 보임
similar_to: 앞 번호 중 대상과 구도가 거의 같은 이미지 번호(없으면 0).
그리고 worst: 시청자가 보기에 가장 품질이 낮거나 뻔한 이미지 번호를 나쁜 순서로 최대 3개.
반드시 JSON 하나:
{{"items": [{{"i": 1, "sunset": false, "filler": false, "blank_screen": false, "neon_float": false, "fake_text": false,
"artifacts": false, "subject_missing": false, "western": false, "similar_to": 0, "reason": "한국어 한 줄"}}, …],
"worst": [3, 7]}}
"""

FLAGS = ("sunset", "filler", "blank_screen", "neon_float", "fake_text", "artifacts", "subject_missing", "western")
SEVERE = ("fake_text", "artifacts", "subject_missing", "blank_screen", "neon_float")


def review(images: list[str | Path], hints: list[str], read_fn=None) -> dict:
    """{"items": [{"i", 결함 FLAGS..., "similar_to", "reason", "bad"}], "worst": [...]} (실패하면 {"items": [], "worst": []})."""
    empty = {"items": [], "worst": []}
    if not images:
        return empty
    if read_fn is None:
        from recap_shorts.llm.codex import VISION_MODEL, read_images

        def read_fn(imgs, instr):
            return read_images(imgs, instr, VISION_MODEL, json=True)
    lines = "\n".join(f"{k + 1}. {h}" for k, h in enumerate(hints))
    try:
        out = read_fn(list(images), INSTRUCTION.format(n=len(images), hints=lines))
    except Exception:  # noqa: BLE001
        return empty
    items = out.get("items") if isinstance(out, dict) else None
    if not isinstance(items, list):
        return empty
    by_i = {}
    for it in items:
        if isinstance(it, dict):
            try:
                by_i[int(it.get("i"))] = it
            except (TypeError, ValueError):
                continue
    n = len(images)
    worst = []
    for w in (out.get("worst") or []):
        try:
            w = int(w)
        except (TypeError, ValueError):
            continue
        if 1 <= w <= n and w not in worst:
            worst.append(w)
    rows = []
    for k in range(n):
        it = by_i.get(k + 1, {})
        row = {"i": k + 1, **{f: it.get(f) is True for f in FLAGS}, "reason": str(it.get("reason") or "")[:120]}
        try:
            sim = int(it.get("similar_to") or 0)
        except (TypeError, ValueError):
            sim = 0
        row["similar_to"] = sim if 1 <= sim <= k else 0
        row["bad"] = (sum(2 if row[f] else 0 for f in SEVERE) + sum(1 for f in FLAGS if f not in SEVERE and row[f])
                      + (1 if row["similar_to"] else 0) + (1 if (k + 1) in worst else 0))
        rows.append(row)
    return {"items": rows, "worst": worst[:3]}


def pick_redo(result: dict, max_redo: int = 3) -> list[int]:
    """다시 만들 이미지의 0-기준 위치(나쁜 순): 심한 결함이 있거나, 가벼운 결함·겹침이 2개 이상인 장."""
    rows = result.get("items") or []
    cand = [r for r in rows if any(r[f] for f in SEVERE) or r["bad"] >= 2]
    cand.sort(key=lambda r: -r["bad"])
    return [r["i"] - 1 for r in cand[:max(0, max_redo)]]
