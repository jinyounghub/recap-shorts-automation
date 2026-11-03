"""대본이 말하는 '눈으로 보고 싶은 것'(캐릭터·작품·제품·장소·동물·브랜드·음식·행사)의 실제 사진을 찾아 그 문장에 보여 준다.

대본에서 캐릭터 이야기가 나오면 그 캐릭터 사진을 찾아 넣는 식이다(말한 대상을 화면에도 보여 준다).

흐름
  1) extract  : Codex 텍스트 1회 — 대본 문장 + 캡처가 이미 보여 주는 것 → 최대 3개 {sentence, name_ko, ...}
                실존 인물(real_person)은 초상권·퍼블리시티 문제로 통째로 건너뛴다.
  2) 후보 찾기 : 위키백과 REST 요약(ko→en→ja, 없으면 검색 1회) → 위키미디어 공용 검색(photos.search_commons)
                (네이버 이미지 검색은 쓰지 않음 — 결과의 저작권이 확인되지 않음)
                구글·빙 이미지 긁기, 로그인 페이지는 쓰지 않는다. 요청은 HttpClient(정직한 UA·도메인당 3초)로.
  3) 확인     : Codex 비전 1회 — 이름 일치(yes)·또렷함·실사 얼굴 없음·글자 위주 아님. 대상 하나당 최대 2회.
  4) 기록·카드 : post/ref_<k>.<ext>(원본 그대로) + assets/cap/r<k>.png(캡처 카드, label 없음) + 출처(page_url·image_url·site·licence·retrieved_at)

Codex 호출 합계 ≤ 1 + 2×대상(≤7). 키는 출력·저장하지 않는다. 시험 중(PYTEST_CURRENT_TEST)에는 실제 Codex 를 부르지 않는다.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlencode

from recap_shorts import errors

MODEL = None  # inherit the current Codex model
MAX_ITEMS = 3
MAX_VERIFY = 2                     # 대상 하나당 비전 확인 최대 횟수
MIN_SIDE = 200                     # 이보다 작은 그림은 확인 없이 버림(영상에서 뭉개짐). 위키백과 만화 표지가 240px 안팎이라 낮춤
MAX_BYTES = 15_000_000
LANGS = ("ko", "en", "ja")
KINDS = ("character", "work", "product", "place", "animal", "brand", "food", "event", "person")
NONFREE_WIKI = "non-free (Wikipedia fair use)"
CACHE_REL = "assets/entities.json"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ------------------------------------------------------------------ 1) 추출
def _extract_prompt(sentences: list[dict], topic: str, shown: list[str]) -> str:
    lines = "\n".join(f"{s['index']}: {s['text']}" for s in sentences)
    shown_txt = "\n".join(f"- {x}" for x in shown) or "- (없음)"
    return (
        "너는 한국어 숏폼 영상 편집자다. 아래 내레이션을 듣는 시청자가 '그게 뭔지 눈으로 보고 싶어 할' 구체적인 대상을 고른다.\n"
        "대상 종류(kind): character(캐릭터) | work(작품·영화·만화·게임) | product(제품) | place(장소·건물) | animal(동물 종) | "
        "brand(브랜드) | food(특정 음식) | event(행사·사건) | person(실존 인물).\n\n"
        f"영상 제목/주제: {topic}\n\n"
        f"내레이션(번호: 문장, 0번은 첫 줄):\n{lines}\n\n"
        f"화면 캡처가 이미 보여 주는 것(이런 것은 다시 찾지 않는다):\n{shown_txt}\n\n"
        "규칙:\n"
        "- 이름이 분명한 구체적인 것만 고른다(예: '해달', '피카츄'). '자동차', '음식', '사람들' 같은 일반 명사는 제외.\n"
        "- 위 캡처가 이미 보여 주는 대상은 제외.\n"
        "- 실존 인물(역사 속 인물, 연예인, 정치인, 선수 포함)은 real_person=true 로 표시한다(그림을 찾지 않고 건너뜀).\n"
        "- 최대 3개. 시청자가 가장 보고 싶어 할 순서. sentence 는 그 대상이 처음 말해지는 문장의 번호.\n"
        "- name_en 은 위키백과 제목으로 쓸 공식 영어 이름, name_ja 는 일본 작품·캐릭터일 때만(없으면 빈 문자열).\n"
        "- query_hints: 이미지 검색에 도움 되는 짧은 낱말 0~3개. why: 왜 보여 줘야 하는지 한 줄.\n\n"
        'JSON 만 답한다: {"items":[{"sentence":6,"name_ko":"","name_en":"","name_ja":"","kind":"character",'
        '"real_person":false,"query_hints":[""],"why":""}]}  해당하는 것이 없으면 {"items":[]}'
    )


def _validate_items(raw, valid_index: set[int], shown: list[str]) -> list[dict]:
    items = raw.get("items") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    shown_blob = "\n".join(shown)
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            sent = int(it.get("sentence"))
        except (TypeError, ValueError):
            continue
        ko, en, ja = (str(it.get(k) or "").strip() for k in ("name_ko", "name_en", "name_ja"))
        kind = str(it.get("kind") or "").strip().lower()
        if sent not in valid_index or not (ko or en) or kind not in KINDS:
            continue
        if it.get("real_person") is True or kind == "person":
            continue                                    # 실존 인물은 쓰지 않는다
        key = (ko or en).lower()
        if key in seen:
            continue
        if ko and ko in shown_blob:                     # 캡처 요약에 이미 나오는 대상
            continue
        seen.add(key)
        hints = [str(h).strip() for h in (it.get("query_hints") or []) if str(h).strip()][:3] if isinstance(it.get("query_hints"), list) else []
        out.append({"sentence": sent, "name_ko": ko, "name_en": en, "name_ja": ja, "kind": kind,
                    "real_person": False, "query_hints": hints, "why": str(it.get("why") or "").strip()[:200]})
        if len(out) >= MAX_ITEMS:
            break
    return out


def _default_text_fn(prompt: str):
    if "PYTEST_CURRENT_TEST" in os.environ:
        raise RuntimeError("시험 중에는 Codex 를 부르지 않는다")
    from recap_shorts.llm.codex import generate_text
    return generate_text(prompt, MODEL, json=True)


def extract(script: list[dict], topic: str, shown: list[str], text_fn=None) -> list[dict]:
    """script: [{"index": 타이밍 index, "text": 문장}, ...]. 실패하면 []."""
    sentences = [{"index": int(s["index"]), "text": str(s["text"])} for s in script if s.get("text")]
    if not sentences:
        return []
    try:
        raw = (text_fn or _default_text_fn)(_extract_prompt(sentences, topic, shown))
        return _validate_items(raw, {s["index"] for s in sentences}, shown)
    except Exception:  # noqa: BLE001
        return []


# ------------------------------------------------------------------ 2) 후보 찾기
@dataclass
class Cand:
    page_url: str
    image_url: str
    thumb_url: str = ""
    site: str = ""
    licence: str = ""
    license_url: str = ""
    artist: str = ""
    title: str = ""
    source: str = "wikipedia"          # wikipedia | commons


def parse_summary(data: dict, lang: str) -> list[Cand]:
    """위키백과 REST 요약 → 후보(원본, 썸네일). 동음이의 문서·그림 없는 문서는 []."""
    if not isinstance(data, dict) or data.get("type") == "disambiguation":
        return []
    orig = ((data.get("originalimage") or {}).get("source") or "").strip()
    thumb = ((data.get("thumbnail") or {}).get("source") or "").strip()
    if not (orig or thumb):
        return []
    if orig.lower().endswith(".svg"):                 # SVG 원본은 못 쓴다 → 썸네일(PNG)
        orig = ""
    orig, thumb = (u.split("?", 1)[0] for u in (orig, thumb))      # utm 꼬리표 제거
    page = (((data.get("content_urls") or {}).get("desktop") or {}).get("page")
            or f"https://{lang}.wikipedia.org/wiki/{quote(str(data.get('title') or ''))}")
    c = Cand(page_url=page, image_url=orig or thumb, thumb_url=thumb if orig and thumb != orig else "",
             site=f"{lang}.wikipedia.org", title=str(data.get("title") or ""), source="wikipedia")
    return [c]


def _get_json(client, url: str, check_robots: bool = False):
    from recap_shorts.collect.http import Blocked
    r = client.fetch(url, check_robots=check_robots)
    if isinstance(r, Blocked):
        return None
    try:
        return json.loads(r.text)
    except ValueError:
        return None


def wiki_summary(client, lang: str, title: str) -> list[Cand]:
    t = title.strip().replace(" ", "_")
    if not t:
        return []
    data = _get_json(client, f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{quote(t, safe='')}")
    return parse_summary(data, lang) if data else []


def wiki_search_title(client, lang: str, query: str) -> str | None:
    params = {"action": "query", "list": "search", "srsearch": query, "srlimit": 1, "format": "json"}
    data = _get_json(client, f"https://{lang}.wikipedia.org/w/api.php?{urlencode(params)}")
    hits = ((data or {}).get("query") or {}).get("search") or []
    return hits[0].get("title") if hits else None


def wikipedia_candidates(client, ent: dict):
    titles = {"ko": ent.get("name_ko"), "en": ent.get("name_en"), "ja": ent.get("name_ja")}
    for lang in LANGS:
        t = (titles.get(lang) or "").strip()
        if not t:
            continue
        cands = wiki_summary(client, lang, t)
        if not cands:                                   # 없음/동음이의 → 그 언어로 검색 1회
            top = wiki_search_title(client, lang, t)
            if top and top.replace(" ", "_") != t.replace(" ", "_"):
                cands = wiki_summary(client, lang, top)
        yield from cands


def commons_candidates(client, ent: dict):
    from recap_shorts.collect.photos import search_commons
    queries = []
    for q in (ent.get("name_en"), ent.get("name_ko")):
        if q and q not in queries:
            queries.append(q)
    for q in queries[:2]:
        try:
            photos = search_commons(q, limit=4, client=client)
        except Exception:  # noqa: BLE001
            continue
        for p in photos:
            yield Cand(page_url=p.page_url, image_url=p.thumb_url or p.file_url, thumb_url="",
                       site="commons.wikimedia.org", licence=p.license, license_url=p.license_url,
                       artist=p.artist, title=p.title, source="commons")


_COMMONS_FILE_RE = re.compile(r"/wikipedia/commons/(?:thumb/)?[0-9a-f]/[0-9a-f]{2}/([^/]+)")


def resolve_licence(client, cand: Cand) -> Cand:
    """위키백과 그림의 라이선스: 공용(commons) 파일이면 공용 메타데이터에서, 각 언어판 로컬 파일이면 non-free(공정 이용)로 적는다."""
    if cand.source != "wikipedia" or cand.licence:
        return cand
    m = _COMMONS_FILE_RE.search(cand.image_url or "")
    if not m:
        cand.licence = NONFREE_WIKI if re.search(r"upload\.wikimedia\.org/wikipedia/(ko|en|ja)/", cand.image_url or "") else "unknown"
        return cand
    from urllib.parse import unquote
    name = unquote(m.group(1))
    params = {"action": "query", "format": "json", "titles": "File:" + name, "prop": "imageinfo", "iiprop": "extmetadata|url"}
    data = _get_json(client, "https://commons.wikimedia.org/w/api.php?" + urlencode(params))
    try:
        page = next(iter(((data or {}).get("query") or {}).get("pages", {}).values()))
        ii = page["imageinfo"][0]
        meta = ii.get("extmetadata") or {}
        cand.licence = (meta.get("LicenseShortName") or {}).get("value") or "Wikimedia Commons (라이선스 확인 필요)"
        cand.license_url = (meta.get("LicenseUrl") or {}).get("value", "")
        from recap_shorts.collect.photos import strip_html
        cand.artist = strip_html((meta.get("Artist") or {}).get("value", ""))
        cand.page_url = ii.get("descriptionurl") or cand.page_url
    except (StopIteration, KeyError, IndexError, TypeError):
        cand.licence = "Wikimedia Commons (라이선스 확인 필요)"
    return cand


# ------------------------------------------------------------------ 3) 확인
def _verify_prompt(ent: dict) -> str:
    names = " / ".join(x for x in (ent.get("name_ko"), ent.get("name_en"), ent.get("name_ja")) if x)
    return (
        f"이 그림이 '{names}'({ent.get('kind')})을(를) 보여 주는지 확인한다.\n"
        "- is_match: 그림의 주인공/대상이 정말 그것이면 \"yes\", 아니거나 모르겠으면 \"no\".\n"
        "- clear: 대상이 또렷하게 보이면 true (너무 작거나 가려졌거나 흐리면 false).\n"
        "- real_face: 실제 사람의 얼굴이 사진·실사로 보이면 true (만화·캐릭터·그림·동물은 false).\n"
        "- mostly_text: 글자 위주의 그림(웹페이지·문서·스크린샷·표)이면 true.\n"
        'JSON 만 답한다: {"is_match":"yes","clear":true,"real_face":false,"mostly_text":false,"note":"한 줄"}'
    )


def _default_vision_fn(image_bytes: bytes, instruction: str):
    if "PYTEST_CURRENT_TEST" in os.environ:
        raise RuntimeError("시험 중에는 Codex 를 부르지 않는다")
    from recap_shorts.llm.codex import read_images
    return read_images([image_bytes], instruction, MODEL, json=True)


def accept_verdict(v) -> bool:
    if not isinstance(v, dict):
        return False
    yes = str(v.get("is_match")).strip().lower() in ("yes", "true")
    return yes and v.get("clear") is True and v.get("real_face") is False and v.get("mostly_text") is False


# ------------------------------------------------------------------ 4) 내려받기·기록
def _download(client, url: str, check_robots: bool) -> bytes | None:
    from recap_shorts.collect.http import Blocked
    if not url:
        return None
    try:
        r = client.fetch(url, check_robots=check_robots, use_cache=False)
    except Exception:  # noqa: BLE001
        return None
    if isinstance(r, Blocked) or len(r.content) > MAX_BYTES:
        return None
    return r.content


def _open_ok(data: bytes):
    """(확장자, (w, h)) — 열리지 않거나 너무 작으면 None. GIF·기타는 '.png' 로 바꿔 저장해야 하므로 None 확장자는 변환 표시."""
    from PIL import Image
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            fmt = (im.format or "").upper()
            size = im.size
    except Exception:  # noqa: BLE001
        return None
    if min(size) < MIN_SIDE:
        return None
    return {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}.get(fmt, ".png"), size, fmt


def _save(data: bytes, ext: str, fmt: str, dest_noext: Path) -> Path:
    from PIL import Image
    dest = dest_noext.with_suffix(ext)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if fmt in ("JPEG", "PNG", "WEBP"):
        dest.write_bytes(data)
    else:                                               # GIF 등은 첫 장면을 PNG 로
        Image.open(io.BytesIO(data)).convert("RGB").save(dest)
    return dest


@dataclass
class Stats:
    calls: int = 0
    log: list[str] = field(default_factory=list)


def find_for_entity(ent: dict, k: int, job_dir: Path, client, stats: Stats, vision_fn=None, *, sources=None) -> dict | None:
    """대상 하나의 사진을 찾아 post/ref_<k>.<ext> 로 저장하고 {"file", "ref"} 를 돌려준다. 못 찾으면 None."""
    vision_fn = vision_fn or _default_vision_fn
    sources = sources or [lambda: wikipedia_candidates(client, ent), lambda: commons_candidates(client, ent)]
    verifies, seen = 0, set()
    for make in sources:
        try:
            gen = make()
            for cand in gen:
                if verifies >= MAX_VERIFY:
                    return None
                if cand.image_url in seen:
                    continue
                seen.add(cand.image_url)
                got = None
                for url in (cand.image_url, cand.thumb_url):
                    data = _download(client, url, check_robots=False)   # 위키 API·upload 는 공식 API(robots 확인 안 함)
                    meta = _open_ok(data) if data else None
                    if meta:
                        got = (url, data, meta)
                        break
                if not got:
                    stats.log.append(f"{ent.get('name_ko')}: 내려받기·크기 실패 {cand.site}")
                    continue
                url, data, (ext, size, fmt) = got
                verifies += 1
                stats.calls += 1
                try:
                    verdict = vision_fn(data, _verify_prompt(ent))
                except Exception as e:  # noqa: BLE001
                    stats.log.append(f"{ent.get('name_ko')}: 확인 호출 실패 — {errors.reason(e)}")
                    continue
                if not accept_verdict(verdict):
                    stats.log.append(f"{ent.get('name_ko')}: {cand.site} 그림 탈락 ({str((verdict or {}).get('note') if isinstance(verdict, dict) else '')[:60]})")
                    continue
                cand.image_url = url
                resolve_licence(client, cand)
                dest = _save(data, ext, fmt, job_dir / "post" / f"ref_{k}")
                ref = {"name": ent.get("name_ko") or ent.get("name_en"), "kind": ent.get("kind"), "page_url": cand.page_url,
                       "image_url": cand.image_url, "site": cand.site, "licence": cand.licence,
                       "licence_url": cand.license_url, "artist": cand.artist, "source": cand.source,
                       "retrieved_at": _now(), "verified_by": MODEL}
                return {"file": dest.relative_to(job_dir).as_posix(), "ref": ref}
        except Exception as e:  # noqa: BLE001
            stats.log.append(f"{ent.get('name_ko')}: {errors.reason(e)}")
            continue
    return None


# ------------------------------------------------------------------ 전체
def _cache_key(sentences: list[dict]) -> str:
    return hashlib.sha1(json.dumps([(s["index"], s["text"]) for s in sentences], ensure_ascii=False).encode()).hexdigest()[:16]


def shown_from_topic(topic: dict) -> list[str]:
    return [str(a.get("summary")).strip() for a in (topic.get("attachments") or []) if a.get("summary")]


def find_entity_images(job_dir: Path | str, script: dict, timings: dict, topic: dict, client, *,
                       text_fn=None, vision_fn=None, sources_for=None, make_card=None) -> dict:
    """작업 폴더 안에서 전부 수행. 돌려주는 값:
    {"entries": [assets.json 항목], "found": [이름], "missed": [이름], "extracted": [...], "calls": 사용한 Codex 호출 수, "log": [...]}
    같은 대본으로 다시 부르면(전부 찾았던 경우만) assets/entities.json 의 결과를 그대로 쓴다(호출 0회)."""
    job_dir = Path(job_dir)
    sentences = [{"index": s["index"], "text": s["text"]} for s in timings["sentences"]]
    key = _cache_key(sentences)
    cp = job_dir / CACHE_REL
    if cp.is_file():
        try:
            old = json.loads(cp.read_text(encoding="utf-8"))
            if old.get("key") == key and all((job_dir / e["file"]).is_file() and (job_dir / e["src"]).is_file() for e in old.get("entries", [])):
                return {**old, "calls": 0, "cached": True}
        except (ValueError, KeyError, OSError):
            pass
    if make_card is None:
        from recap_shorts.render.cards import make_card
    topic_line = " ".join(script.get("title_lines") or []) + (" / " + script["hook"] if script.get("hook") else "")
    shown = shown_from_topic(topic)
    stats = Stats()
    ents = extract(sentences, topic_line, shown, text_fn=text_fn)
    stats.calls += 1
    entries, found, missed = [], [], []
    for k, ent in enumerate(ents, start=1):
        name = ent["name_ko"] or ent["name_en"]
        sources = sources_for(ent) if sources_for else None
        hit = find_for_entity(ent, k, job_dir, client, stats, vision_fn, sources=sources)
        if not hit:
            missed.append(name)
            continue
        out = job_dir / "assets" / "cap" / f"r{k}.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            make_card(job_dir / hit["file"], out, label=None)
        except Exception as e:  # noqa: BLE001
            stats.log.append(f"{name}: 카드 실패 — {errors.reason(e)}")
            missed.append(name)
            continue
        found.append(name)
        entries.append({"file": out.relative_to(job_dir).as_posix(), "kind": "cap", "role": "focus", "sentence": ent["sentence"],
                        "src": hit["file"], "ref": hit["ref"], "status": "ok", "unblurred_risk": False,
                        "attachment": None, "crop": None, "blur_boxes": [], "blurred": False, "needs_review": False,
                        "review_reason": f"대상 사진({hit['ref']['site']}, {hit['ref']['licence']}) — 출처·라이선스 확인",
                        "entity": {k2: ent[k2] for k2 in ("name_ko", "name_en", "name_ja", "kind", "why")}})
    res = {"key": key, "entries": entries, "found": found, "missed": missed, "extracted": ents, "calls": stats.calls,
           "log": stats.log}
    if entries and not missed:                          # 일부러 못 찾은 경우·추출 실패는 저장하지 않아 다시 돌리면 재시도
        try:
            cp.parent.mkdir(parents=True, exist_ok=True)
            cp.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
    return res
