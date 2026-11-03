"""Codex native imagegen task handoff; no image HTTP client or paid fallback."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# 공통 규칙: 사진처럼 사실적, 4:3 가로, 스톡 사진 티 금지, 실존 인물 닮게 금지, 글자는 설명이 따옴표로 준 짧은 한글만.
# 참고한 쇼츠의 AI 그림은 '문장이 말한 그 장면'을 정면으로 보여 주고(한국 단서 40%, 표정이 읽히는 지어낸 사람 14%,
# 읽히는 한글 8.5%), 분위기·소품 위주 그림보다 내용이 잘 전달됐다.
COMMON_PREFIX = ("Photorealistic camera photograph, horizontal landscape composition, target aspect ratio 4:3 or the "
                 "closest available landscape ratio. ")
COMMON_SUFFIX = (" Candid editorial news-photo look: the real lighting of that place (fluorescent, flash, overcast "
                 "daylight), realistic everyday wear and clutter, not staged stock photography. Avoid stock clichés: no "
                 "sunset or golden-hour glow unless the scene needs it, no decorative potted plants, tea cups or cozy "
                 "living-room filler, no neon glow, no floating icons or speech bubbles, no blank glowing screens. "
                 "Natural photographic lighting and realistic materials, documentary photography, not an illustration "
                 "or render. No real or recognizable people and no likeness of any real person, celebrity, athlete or "
                 "politician; any person shown is a fictional ordinary person, otherwise show the back view or just hands. "
                 "Absolutely no text, letters, numbers, logos, brands, team emblems or watermarks anywhere, except a "
                 "short Korean word only when the description quotes it exactly; all other surfaces are clean and unmarked.")

HINT_INSTRUCTION = """아래는 한국어 쇼츠의 문장별 (내레이션)과 '화면 설명'(image_hint) 목록이다. 각 항목을 사진 이미지 생성용 영어 장면 묘사로 바꿔라.
- 항목마다 1~2문장. **그 문장이 말한 사건을 문자 그대로**: 누가·무엇을·어디서. 주인공 하나가 화면 가운데 60~80%를 채운다.
  분위기·상징으로 바꾸지 말 것(예: '돈 문제' → 지갑 소품 X, 문장의 그 영수증·그 창구 O).
- 장소는 이야기의 실제 나라 그대로. 한국 이야기면 한국 단서 하나를 꼭 넣는다: 한국 아파트·병원 원무과·편의점·지하철·
  관공서·한국 번호판·원화 지폐·한국 강변 등. 서양식 도시 풍경·서양식 거실·서양 병원 금지.
- 뻔한 스톡 사진 금지: 노을·골든아워, 화분, 찻잔, 아늑한 거실, 빈 화면이 빛나는 스마트폰, 네온 빛, 공중에 뜬 말풍선·아이콘,
  '스마트폰을 든 손'(문장에 휴대폰이 실제로 나올 때만).
- 조명은 문장의 분위기에 맞춘다: 사실 전달 = 밝은 낮빛, 위기·손해·충격 = 어둡고 붉은 톤, 반전·결론 = 위에서 내리는 스포트라이트.
- 사람: 놀람·걱정·분노·어색함 같은 감정이 핵심인 문장은 "an ordinary Korean adult (fictional), medium shot, clearly <감정>
  facial expression"처럼 표정이 보이는 지어낸 보통 사람을 써도 된다. 단, 이야기 속 실제 인물(글쓴이·기사 속 사람·연예인·
  선수·정치인)의 대역처럼 보이면 안 된다 — '그 소식을 본 보통 사람'의 반응으로만. 그 밖의 문장은 사람 없이 또는 뒷모습·손.
- 글자: 핵심 낱말이 짧은 한글(6자 이하)이고 도장·손글씨 쪽지·표지판에 자연스럽게 들어갈 수 있으면
  "a rubber stamp that reads '…' in Korean"처럼 따옴표로 정확히 적는다. 그 밖에는 글자가 안 보이는 구도
  (영수증·화면·간판은 비스듬한 각도나 얕은 초점).
- 앞뒤 문장이 같은 사건을 말하면 같은 주인공·같은 장소를 각도만 바꿔 이어 간다("same <대상>, closer / different angle /
  aftermath"). 다른 사건이면 대상과 구도를 겹치지 말 것(클로즈업·중간·넓은 장면을 섞는다).
- 반드시 JSON 객체 하나: {"prompts": ["영어 묘사 1", "영어 묘사 2", ...]} — 입력과 같은 개수·같은 순서.

[목록]
"""


def build_prompt(hint: str) -> str:
    """영어 장면 묘사(hint) 앞뒤에 공통 규칙을 붙인다."""
    return COMMON_PREFIX + hint.strip().rstrip(".") + "." + COMMON_SUFFIX


def hints_to_prompts(hints: list[str], text_fn=None) -> list[str]:
    """한국어 image_hint 들을 영어 장면 묘사로(Codex OAuth 호출 1회). 개수가 안 맞으면 예외."""
    if not hints:
        return []
    if text_fn is None:
        from recap_shorts.llm.codex import generate_text

        def text_fn(p):
            return generate_text(p, json=True)
    lines = "\n".join(f"{i + 1}. {h}" for i, h in enumerate(hints))
    out = text_fn(HINT_INSTRUCTION + lines)
    prompts = out.get("prompts") if isinstance(out, dict) else None
    if not isinstance(prompts, list) or len(prompts) != len(hints):
        raise RuntimeError(f"영어 프롬프트 개수가 맞지 않음(요청 {len(hints)}, 응답 {len(prompts) if isinstance(prompts, list) else '없음'})")
    return [str(p).strip() for p in prompts]


def engine() -> str:
    from recap_shorts import settings
    value = settings.image_engine()
    if value not in ("codex", "off"):
        raise ValueError("이미지 엔진은 codex 또는 off만 사용할 수 있습니다")
    return value


def engine_detail() -> str:
    return ("AI 그림 꺼짐 — 캡처 카드만" if engine() == "off" else
            "Codex 내장 imagegen 작업 대기 — 현재 Codex 세션에서 생성")


def available() -> bool:
    """Task handoff is enabled; this does not attest tool availability in Codex."""
    return engine() == "codex"


def engine_status() -> tuple[bool, str, str]:
    return available(), engine(), engine_detail()


def generate(prompt: str, out_png: str | Path, aspect: str = "landscape") -> dict:
    if engine() == "off":
        return {"ok": False, "file": str(out_png), "seconds": 0.0,
                "pending": False, "error": "AI 그림 생성이 꺼져 있습니다(image_engine=off)"}
    from .codex_imagegen import request
    return request(prompt, out_png, aspect)


def generate_many(items: list[dict], workers: int = 2, on_done=None, gen=None) -> list[dict]:
    """Register tasks and inspect existing files; never synthesize image pixels."""
    gen = gen or generate
    results: list[dict | None] = [None] * len(items)

    def one(i):
        result = gen(items[i]["prompt"], items[i]["out"], items[i].get("aspect", "landscape"))
        results[i] = result
        if on_done:
            on_done(i, result)

    with ThreadPoolExecutor(max_workers=max(1, min(workers, 2))) as pool:
        list(pool.map(one, range(len(items))))
    return results
