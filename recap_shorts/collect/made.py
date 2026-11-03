"""로컬 work/<id>/state.json 에서 이미 제작한 소재를 찾는다.

외부 보관 색인이나 경로를 검색하지 않는다. 글의 원본 주소도 해당 작업 폴더의
post/post_<번호>.json 에서만 읽는다.
  - 목록 글과 견주는 방법(좋은 순서):
      same_post     같은 갤러리·같은 글 번호                     → "만든 적 있음"
      same_original 원본 갤러리 글(origin_url)이 같음             → 목록에는 원본 주소가 없어서 주소를 아는 때만(아래 match 의 origin_url)
      similar       제목을 정리한 뒤 글자 2개 묶음(bigram) 겹침이 SIMILAR_THRESHOLD 이상 → "비슷한 이야기 만듦"
  - 제목 정리: [싱갤] 같은 앞 태그, 싱글벙글·와들와들 같은 말머리, .jpg·.manhwa 같은 파일 꼬리표, ㅋㅋ·ㄷㄷ 같은 자음 웃음, 문장부호를 뺀다.

다른 채널의 영상은 검색하지 않는다. 제목이 전혀 다른 재작성 글은 놓칠 수 있다.
"""
from __future__ import annotations

import json
import re
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

SIMILAR_THRESHOLD = 0.22       # 글자 2개 묶음 Jaccard — 우리 job 15개 × 실제 목록 149개 + 목록끼리 11,026쌍을 점검해 정했다
MIN_NORM_LEN = 5               # 정리한 제목이 이보다 짧으면 비교하지 않는다(짧은 제목은 우연히 겹치기 쉬움)
MIN_SHARED = 3                 # 겹치는 글자 2개 묶음이 이보다 적으면 점수가 높아도 '비슷함'으로 보지 않는다

# 비교에서 뺄 말머리·군더더기 — 긴 것부터 지운다(안싱글벙글 → 싱글벙글 순서가 맞아야 함)
NOISE_WORDS = ("안싱글벙글", "싱글벙글", "와들와들", "훌쩍훌쩍", "두근두근", "실시간", "근황", "레전드", "역대급")
_NOISE = re.compile("|".join(sorted(map(re.escape, NOISE_WORDS), key=len, reverse=True)))
_LEAD_TAGS = re.compile(r"^\s*(?:\[[^\]]{1,12}\]\s*|\([^)]{1,8}\)\s*)+")        # [싱갤] [속보] (스압?) — 앞쪽만
_LEAD_SOURCE = re.compile(r"^\s*[A-Za-z0-9가-힣ㄱ-ㅎ]{1,6}\)\s*")                  # "블라인드) " "MBC) " "ㄱㄷ) " 같은 출처 머리말
_FILE_TAIL = re.compile(r"(?i)\.?(?:jpe?g|png|gif|webp|mp4|webm|manhwa|manga|gifv|txt)\b")   # .jpg .JPG .manhwa (점 없이 붙은 'jpg'도)
_JAMO = re.compile(r"[ㄱ-ㅣ]+")                                                    # ㅋㅋ ㄷㄷ ㅎㄷㄷ ㅠㅠ ㅇㅇ — 자음·모음만 있는 덩어리
_NON_WORD = re.compile(r"[^0-9a-z가-힣]+")


# ---------------------------------------------------------------- 제목 정리·비교
def normalize_title(title: str | None) -> str:
    """제목 비교용으로 정리한 글자(공백 없음). 태그·말머리·파일 꼬리표·자음 웃음·문장부호를 뺀 소문자 한글·영숫자만 남는다."""
    s = _JAMO.sub(" ", title or "")           # NFKC 가 호환 자모(ㅋ)를 다른 코드로 바꾸기 전에 먼저 뺀다
    s = unicodedata.normalize("NFKC", s)
    s = _LEAD_TAGS.sub("", s)
    s = _LEAD_SOURCE.sub("", s)
    s = _FILE_TAIL.sub(" ", s)
    s = _NOISE.sub(" ", s.lower())
    return _NON_WORD.sub("", s)


def bigrams(norm: str) -> frozenset[str]:
    return frozenset(norm[i:i + 2] for i in range(len(norm) - 1))


def similarity(a: str | None, b: str | None) -> float:
    """두 제목의 정리한 글자 2개 묶음 Jaccard(0~1). 정리한 제목이 MIN_NORM_LEN 보다 짧으면 0."""
    na, nb = normalize_title(a), normalize_title(b)
    if len(na) < MIN_NORM_LEN or len(nb) < MIN_NORM_LEN:
        return 0.0
    ga, gb = bigrams(na), bigrams(nb)
    inter = len(ga & gb)
    return inter / len(ga | gb) if inter >= MIN_SHARED else 0.0


def origin_key(url: str | None) -> tuple[str, int] | None:
    """원본 갤러리 글 주소 → (갤러리 id, 글 번호). 읽을 수 없으면 None."""
    if not url:
        return None
    try:
        q = parse_qs(urlparse(url.strip()).query)
        return q["id"][0], int(q["no"][0])
    except (KeyError, IndexError, ValueError):
        return None


def post_key(gallery: str | None, no) -> tuple[str, str]:
    """글 하나를 가리키는 색인 열쇠 — 글 번호는 문자열로 둔다(디시는 숫자, 블라인드는 8자 별칭)."""
    return str(gallery or ""), str(no or "").strip()


def fmt_when(iso: str | None) -> str:
    """"2026-01-02T12:38:45+09:00" → "01-02 12:38" (job 은 한국 시각으로 적혀 있어 문자열 그대로 자른다)."""
    m = re.match(r"\d{4}-(\d\d-\d\d)[T ](\d\d:\d\d)", iso or "")
    return f"{m.group(1)} {m.group(2)}" if m else (iso or "")


# ---------------------------------------------------------------- 색인
@dataclass(frozen=True)
class MadeJob:
    job_id: str
    gallery: str
    no: str                    # 디시는 글 번호 숫자, 블라인드는 글 주소 별칭(8자) — 늘 문자열로 맞춘다
    source_title: str          # 원본 글 제목(태그 없이 저장된 것)
    our_title: str             # 우리가 붙인 영상 제목
    created_at: str
    finished: bool             # 영상 만들기(render)까지 끝남 — 아니면 중간에 멈춘 작업
    status: str
    origin_url: str = ""       # 실시간 베스트 글이 퍼온 원본 갤러리 글(post/post_<번호>.json). 모르면 ""
    site: str = "dcinside"     # 글 출처 — 같은 번호라도 다른 사이트면 다른 글이다

    @property
    def origin(self) -> tuple[str, int] | None:
        return origin_key(self.origin_url)


class MadeIndex:
    def __init__(self, jobs: list[MadeJob]):
        self.jobs = sorted(jobs, key=lambda j: j.created_at or "")
        self._by_post: dict[tuple[str, str], list[MadeJob]] = {}
        self._by_origin: dict[tuple[str, int], list[MadeJob]] = {}
        self._grams: list[tuple[MadeJob, frozenset[str], frozenset[str]]] = []      # (job, 원본 제목 묶음, 영상 제목 묶음)
        for j in self.jobs:
            self._by_post.setdefault(post_key(j.gallery, j.no), []).append(j)
            if j.origin:
                self._by_origin.setdefault(j.origin, []).append(j)
            norms = (normalize_title(j.source_title), normalize_title(j.our_title))
            self._grams.append((j, *[bigrams(n) if len(n) >= MIN_NORM_LEN else frozenset() for n in norms]))

    def __len__(self) -> int:
        return len(self.jobs)

    def same_post(self, gallery: str, no) -> list[MadeJob]:
        """같은 갤러리·같은 글 번호로 만든 job 들(오래된 순)."""
        return list(self._by_post.get(post_key(gallery, no), []))

    def same_original(self, origin_url: str | None) -> list[MadeJob]:
        """원본 갤러리 글이 같은 job 들. 원본이 그 글 자체(갤러리 글로 직접 만든 job)인 경우도 같은 원본으로 본다."""
        k = origin_key(origin_url)
        if k is None:
            return []
        return list(self._by_origin.get(k, [])) + [j for j in self._by_post.get(post_key(k[0], k[1]), [])
                                                   if j not in self._by_origin.get(k, [])]

    def similar(self, title: str, threshold: float = SIMILAR_THRESHOLD, exclude: tuple[str, str] | None = None) -> tuple[MadeJob, float] | None:
        """제목이 가장 비슷한 job 과 점수. 우리 원본 제목·영상 제목 둘 다와 견주어 높은 쪽을 쓴다. threshold 미만이면 None."""
        n = normalize_title(title)
        if len(n) < MIN_NORM_LEN:
            return None
        g = bigrams(n)
        best: tuple[MadeJob, float] | None = None
        for j, gs, go in self._grams:
            if exclude and post_key(j.gallery, j.no) == exclude:
                continue
            sc = max((len(g & x) / len(g | x) if len(g & x) >= MIN_SHARED else 0.0) for x in (gs, go))
            if sc >= threshold and (best is None or sc > best[1] or (sc == best[1] and j.created_at > best[0].created_at)):
                best = (j, sc)
        return best

    def match(self, gallery: str, no, title: str, origin_url: str | None = None,
              threshold: float = SIMILAR_THRESHOLD, site: str = "") -> dict | None:
        """목록 글 하나의 '만든 적 있음' 표시. 없으면 None.
        {kind: same_post|same_original|similar, job_id, our_title, created_at, score, count, finished}
        count = 같은 글로 만든 job 수(same_post·same_original 일 때), 대표 job 은 가장 최근 것.
        site 를 주면 같은 글 번호라도 그 사이트 글만 같은 글로 본다(블라인드 별칭이 우연히 겹칠 수 있음)."""
        same = [j for j in self.same_post(gallery, no) if not site or j.site == site]
        if same:
            return _view("same_post", same[-1], 1.0, len(same))
        if origin_url:
            orig = self.same_original(origin_url)
            if orig:
                return _view("same_original", orig[-1], 1.0, len(orig))
        hit = self.similar(title, threshold, exclude=post_key(gallery, no))
        if hit:
            return _view("similar", hit[0], round(hit[1], 3), 1)
        return None


def _view(kind: str, j: MadeJob, score: float, count: int) -> dict:
    return {"kind": kind, "job_id": j.job_id, "our_title": j.our_title, "source_title": j.source_title, "created_at": j.created_at,
            "score": score, "count": count, "finished": j.finished}


# ---------------------------------------------------------------- work/ 에서 색인 만들기
def _read_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _origin_from(job_dir: Path, no) -> str:
    """작업 내부의 post/post_<번호>.json 에서만 원본 주소를 읽는다."""
    try:
        d = job_dir / "post"
        if d.is_symlink():
            return ""
        candidate = d / f"post_{no}.json"
        if candidate.is_symlink() or job_dir.resolve() not in candidate.resolve().parents:
            return ""
        post = _read_json(candidate)
        if post is None:                       # 번호가 달라진 경우를 대비해 폴더의 첫 post_*.json
            first = next((p for p in sorted(d.glob("post_*.json")) if not p.is_symlink()), None)
            post = _read_json(first) if first and job_dir.resolve() in first.resolve().parents else None
    except (OSError, ValueError, TypeError):
        return ""
    return str(post.get("origin_url") or "") if isinstance(post, dict) else ""


def _job_from_state(state: dict, *, job_dir: Path) -> MadeJob | None:
    src = state.get("source") or {}
    no = str(src.get("no") or "").strip()
    if not no:
        return None
    gallery = str(src.get("gallery") or "")
    jid = str(state.get("id") or "")
    if not gallery or not jid:
        return None
    site = str(src.get("site") or "").strip().lower()
    site = site if site in ("blind", "local") else "dcinside"
    steps = {s.get("key"): s.get("status") for s in state.get("steps") or [] if isinstance(s, dict)}
    if steps.get("source") == "error":      # 글을 가져오지도 못하고 실패한 작업은 만든 것으로 치지 않는다(다시 시도할 수 있게)
        return None
    origin = _origin_from(job_dir, no)
    return MadeJob(job_id=jid, gallery=gallery, no=no,
                   source_title=str(src.get("title") or ""),
                   our_title=str(state.get("title") or src.get("title") or ""),
                   created_at=str(state.get("created_at") or ""),
                   finished=steps.get("render") == "done", status=str(state.get("status") or ""), origin_url=origin,
                   site=site)


def _local_states(root: Path) -> list[Path]:
    """작업 폴더 밖을 가리키는 심볼릭 링크를 따라 읽지 않는다."""
    try:
        paths = []
        for directory in sorted(root.iterdir()):
            if directory.is_symlink() or not directory.is_dir():
                continue
            state = directory / "state.json"
            if not state.is_symlink() and state.is_file():
                paths.append(state)
        return paths
    except OSError:
        return []


def _signature(root: Path) -> tuple:
    sig = []
    try:
        for p in _local_states(root):
            st = p.stat()
            sig.append((p.parent.name, st.st_mtime_ns, st.st_size))
    except OSError:
        pass
    return tuple(sig)


_cache: dict[str, tuple[tuple, MadeIndex]] = {}
_lock = threading.Lock()


def build_index(work_root: Path | str) -> MadeIndex:
    """work_root 내부의 state.json 만으로 색인을 새로 만든다(캐시 안 씀)."""
    root = Path(work_root).expanduser().resolve()
    jobs: dict[str, MadeJob] = {}
    for p in _local_states(root):
        st = _read_json(p)
        if not isinstance(st, dict) or st.get("id") != p.parent.name:
            continue
        j = _job_from_state(st, job_dir=p.parent)
        if j:
            jobs[j.job_id] = j
    return MadeIndex(list(jobs.values()))


def get_index(work_root: Path | str) -> MadeIndex:
    """로컬 work/*/state.json 의 수정 시각·크기가 같으면 기존 색인을 쓴다."""
    root = Path(work_root).expanduser().resolve()
    sig = _signature(root)
    key = str(root.resolve())
    with _lock:
        hit = _cache.get(key)
        if hit and hit[0] == sig:
            return hit[1]
    idx = build_index(root)
    with _lock:
        _cache[key] = (sig, idx)
    return idx


def clear_cache() -> None:
    with _lock:
        _cache.clear()


# ---------------------------------------------------------------- 목록에 붙이기
HIDE_KINDS = ("same_post", "same_original")      # hide_made 가 숨기는 종류 — 같은 글(similar 는 표시만 하고 남긴다)


def annotate_items(idx: MadeIndex, items: list[dict], gallery: str, site: str = "") -> dict:
    """목록 항목(dict, no·title 필요)마다 item["made"] 를 붙이고(없으면 None) 개수를 돌려준다. 네트워크 호출 없음."""
    marked = same = 0
    for it in items:
        m = idx.match(gallery, it["no"], it.get("title") or "", it.get("origin_url"), site=site)
        it["made"] = m
        if m:
            marked += 1
            same += m["kind"] in HIDE_KINDS
    return {"made_count": marked, "made_same_count": same}


def drop_made(items: list[dict]) -> list[dict]:
    """hide_made: 같은 글(HIDE_KINDS)로 표시된 항목을 뺀 목록."""
    return [i for i in items if (i.get("made") or {}).get("kind") not in HIDE_KINDS]


# ---------------------------------------------------------------- 알림 문구
def duplicate_message(jobs: list[MadeJob]) -> str:
    """같은 글로 이미 만든 job 들을 이름 붙인 한국어 안내(최신 순, 최대 3개)."""
    jobs = sorted(jobs, key=lambda j: j.created_at or "", reverse=True)
    head = "이미 만든 글입니다" + (f" ({len(jobs)}번)" if len(jobs) > 1 else "")
    lines = [f"{fmt_when(j.created_at)} · {j.our_title}" + ("" if j.finished else " (영상 안 끝남)") for j in jobs[:3]]
    more = f" 외 {len(jobs) - 3}개" if len(jobs) > 3 else ""
    return f"{head}: " + " / ".join(lines) + more
