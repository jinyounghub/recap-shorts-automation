"""디시인사이드 실시간 베스트(dcbest) 목록 수집 — 주제 후보를 찾는 데만 쓴다.

- 목록 주소: https://gall.dcinside.com/board/lists/?id=dcbest&page=N (robots.txt: dcbest 는 `*` 에 허용).
  그래도 http 계층의 robots 확인·3초 간격을 그대로 거친다.
- 줄마다 `<tr class="ub-content" data-no data-type="icon_btimebest">`; 제목은 `<strong>[싱갤]</strong> 제목`(갤러리 태그),
  댓글 수는 `.reply_num` "[39]" 또는 "[61/1]"(앞이 일반 댓글), 시각은 `.gall_date[title]`(KST, 초 단위).
- 작성자 닉네임·아이디·IP 는 읽지도 저장하지도 않는다(BestItem 에 해당 필드가 없음).
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lxml import html as LH

from .dcinside import _fetch, view_url
from .http import HttpClient

KST = timezone(timedelta(hours=9))
LIST_URL = "https://gall.dcinside.com/board/lists/?id=dcbest&page={page}"
CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "topics.json"
SORTS = ("views", "comments", "recent")
_TAG = re.compile(r"^\[(.{1,10})\]$")


class DcbestNotAllowed(Exception):
    """robots.txt 가 실시간 베스트 목록을 허용하지 않음."""


@dataclass
class BestItem:
    no: int
    title: str            # 갤러리 태그를 뺀 제목
    tag: str              # "싱갤" (없으면 "")
    views: int
    recommends: int
    comments: int
    posted_at: str        # ISO 8601, +09:00
    age_hours: float
    url: str              # PC 글 주소 — 만들기(작업 만들기)에 그대로 쓴다


def load_config(path: Path | str | None = None) -> dict:
    try:
        return json.loads(Path(path or CONFIG_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def list_url(page: int = 1) -> str:
    return LIST_URL.format(page=int(page))


def _num(s: str) -> int:
    m = re.search(r"\d[\d,]*", s or "")
    return int(m.group(0).replace(",", "")) if m else 0


def _reply(s: str) -> int:
    """"[39]" → 39, "[61/1]" → 61(앞 숫자가 일반 댓글, 뒤는 보이스리플)."""
    m = re.search(r"\[(\d[\d,]*)", s or "")
    return int(m.group(1).replace(",", "")) if m else 0


def parse_best_list(html: str, now: datetime | None = None) -> list[BestItem]:
    now = now or datetime.now(KST)
    doc = LH.fromstring(html)
    out = []
    for r in doc.xpath('//tr[contains(@class,"ub-content")][@data-no]'):
        if r.get("data-type") == "icon_notice" or not (r.get("data-no") or "").isdigit():
            continue   # 공지·설문·광고 줄
        a = r.xpath('.//td[contains(@class,"gall_tit")]//a[not(contains(@class,"reply_numbox"))][1]')
        if not a:
            continue
        tag = ""
        strong = a[0].xpath('./strong[1]')
        if strong:
            m = _TAG.match(strong[0].text_content().strip())
            tag = m.group(1) if m else ""
            if m:
                strong[0].drop_tree()
        title = re.sub(r"\s+", " ", a[0].text_content()).strip()
        raw = r.xpath('string(.//td[@class="gall_date"]/@title)').strip()
        try:
            at = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
        except ValueError:
            continue   # 시각을 모르면 최근 창 필터를 못 하므로 뺀다
        no = int(r.get("data-no"))
        out.append(BestItem(
            no=no, title=title, tag=tag,
            views=_num(r.xpath('string(.//td[@class="gall_count"])')),
            recommends=_num(r.xpath('string(.//td[@class="gall_recommend"])')),
            comments=_reply(r.xpath('string(.//span[@class="reply_num"])')),
            posted_at=at.isoformat(), age_hours=round(max(0.0, (now - at).total_seconds() / 3600), 1),
            url=view_url("dcbest", no)))
    return out


def sort_items(items: list[BestItem], sort: str = "views") -> list[BestItem]:
    if sort == "comments":
        return sorted(items, key=lambda i: (-i.comments, -i.views))
    if sort == "recent":
        return sorted(items, key=lambda i: i.age_hours)
    return sorted(items, key=lambda i: (-i.views, -i.comments))


def fetch_best(pages: int = 3, hours: float = 48, sort: str = "views", client: HttpClient | None = None,
               now: datetime | None = None, max_pages: int = 5) -> list[BestItem]:
    """1..pages 쪽을 차례로 읽어(쪽 사이 간격은 HttpClient) 최근 hours 시간 안의 글만 sort 순으로 돌려준다."""
    pages = max(1, min(int(pages), max_pages))
    if sort not in SORTS:
        raise ValueError(f"sort 는 {', '.join(SORTS)} 중 하나")
    client = client or HttpClient()
    now = now or datetime.now(KST)
    seen, items = set(), []
    for pg in range(1, pages + 1):
        url = list_url(pg)
        if not client.robots_allowed(url):
            raise DcbestNotAllowed("robots.txt 가 실시간 베스트 목록을 허용하지 않습니다")
        page_items = parse_best_list(_fetch(url, client).text, now)
        for it in page_items:
            if it.no not in seen:
                seen.add(it.no)
                items.append(it)
        if page_items and min(i.age_hours for i in page_items) > hours:
            break   # 이 쪽이 통째로 창 밖이면 뒤 쪽은 더 오래된 글 — 더 읽지 않는다
    return sort_items([i for i in items if i.age_hours <= hours], sort)


# ---------------------------------------------------------------- '출처 확인 필요' 규칙
def source_check(tag: str, title: str, cfg: dict | None = None) -> str | None:
    """원본이 따로 있는 자료를 옮겨 온 글로 보이면 이유(짧은 한국어), 아니면 None. 규칙은 config/topics.json 의 source_check."""
    rules = (cfg if cfg is not None else load_config()).get("source_check", {})
    notes = rules.get("gallery_notes", {})
    if tag and tag in rules.get("galleries", []):
        return f"[{tag}] " + notes.get(tag, "남의 자료를 옮겨 오는 일이 많은 갤러리")
    low = title.lower()
    for m in rules.get("title_markers", []):
        if m.lower() in low:
            return f"제목에 '{m}' — 원본 확인 필요"
    for p in rules.get("title_patterns", []):
        if re.search(p, title, re.I):
            return "제목 꼴이 퍼온 글(방송사 기사·싱글벙글)"
    return None


def to_dict(it: BestItem) -> dict:
    return asdict(it)


# ---------------------------------------------------------------- Clef 분류(글마다 한 번만 — 답을 work/_topics 에 보관)
RISK_TEXT = {"person_attack": "특정인 비난·조롱이 중심", "sexual": "성적 대상화", "unverified_claim": "확인 안 된 의혹"}
CAT_TEXT = {"sports": "스포츠", "life_economy": "생활·경제", "crime": "사건·범죄", "sexual": "선정적",
            "entertainment": "연예", "politics": "정치", "other": "기타"}


def _cache_file(cache_dir: Path, no: int) -> Path:
    return cache_dir / f"triage_{int(no)}.json"


def _read_cache(cache_dir: Path | None, no: int):
    if cache_dir is None:
        return None
    try:
        return json.loads(_cache_file(cache_dir, no).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def triage_items(items: list[BestItem], triage_fn, cache_dir: Path | None, *, batch: int = 8,
                 neuron_budget: float | None = None, spent_fn=None) -> tuple[dict[int, dict], str, str]:
    """-> ({글번호: Clef 분류 결과}, 상태, 설명). 상태: ok | partial | unavailable.

    - 이미 물어본 글(cache_dir/triage_<번호>.json)은 다시 묻지 않는다. 오류가 난 글({"error"})은 저장하지 않아 다음에 다시 묻는다.
    - 안 읽은 글만 batch 개씩 triage_fn(posts) 에 넣는다. None 이 돌아오면(키 없음·하루 한도 429·평가 미통과) 거기서 멈추고,
      그때까지 받은 것은 보관한다 → 분류 없이 목록만 보여 주는 상태.
    - neuron_budget: spent_fn()(이 프로세스가 쓴 뉴런)이 시작 시점보다 이만큼 늘면 멈춘다.
    """
    got: dict[int, dict] = {}
    todo = []
    for it in items:
        c = _read_cache(cache_dir, it.no)
        if c is not None:
            got[it.no] = c
        else:
            todo.append(it)
    if not todo:
        return got, "ok", ""
    start = spent_fn() if spent_fn else 0.0
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
    stopped, why = "", "Clef 를 쓸 수 없어(키 없음·하루 한도·평가 미통과)"
    for i in range(0, len(todo), batch):
        if neuron_budget is not None and spent_fn and spent_fn() - start >= neuron_budget:
            stopped = "budget"
            break
        chunk = todo[i:i + batch]
        res = triage_fn([{"no": it.no, "title": (f"[{it.tag}] " if it.tag else "") + it.title} for it in chunk])
        if not res or all(r.get("error") for r in res):
            # None(키 없음·평가 미통과·하루 한도 3036) 이거나 한 묶음이 모두 오류(하루 무료 한도 429 4006 등) → 더 묻지 않는다
            stopped = "unavailable"
            err = " ".join(str(r.get("error", "")) for r in (res or []))
            if "429" in err or "allocation" in err:
                why = "Codex 사용 한도(429)에 도달해서"
            break
        for r in res:
            no = r.get("no")
            if no is None or r.get("error"):
                continue
            got[no] = r
            if cache_dir is not None:
                _cache_file(cache_dir, no).write_text(json.dumps(r, ensure_ascii=False), encoding="utf-8")
    if stopped == "unavailable":
        return got, ("partial" if got else "unavailable"), why + " 분류 없이 목록만 보여 줍니다"
    if stopped == "budget":
        return got, "partial", "이번 불러오기의 Clef 사용 한도에 닿아 나머지는 분류 없이 보여 줍니다(다시 불러오면 이어서 분류)"
    return got, ("ok" if all(it.no in got for it in items) else "partial"), ""


def topic_view(it: BestItem, tri: dict | None, cfg: dict | None = None) -> dict:
    """화면·API 용 한 줄: 목록 값 + Clef 분야·숨김 + 출처 확인."""
    cat = None
    if tri and tri.get("category"):
        k = max(tri["category"].items(), key=lambda kv: kv[1])[0]
        cat = {"id": k, "label": CAT_TEXT.get(k, k)}
    reasons = [RISK_TEXT.get(r, r) for r in ((tri or {}).get("hidden_by") or [])]
    if cat and cat["id"] == "sexual":
        reasons.append(CAT_TEXT["sexual"])          # 분야가 '선정적'이면 위험 표시가 없어도 접는다(지우지는 않음)
    chk = source_check(it.tag, it.title, cfg)
    return {**asdict(it), "category": cat, "hidden": bool((tri or {}).get("hidden")) or bool(reasons),
            "hidden_reason": reasons, "triaged": tri is not None,
            "source_check": chk is not None, "source_check_reason": chk}
