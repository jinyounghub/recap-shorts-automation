"""로컬 state.json 기반 중복 소재 색인. 외부 보관 경로는 사용하지 않는다."""
import json
from pathlib import Path

import pytest

from recap_shorts.collect import made as M

# ---------------------------------------------------------------- 가짜 job 만들기
def fake_state(jid, gallery, no, src_title, our_title, created_at="2026-10-18T12:00:00+09:00", *, render="done",
               source="done", status="done"):
    keys = ["source", "read", "script", "voice", "assets", "scenes", "render", "review"]
    done_upto = keys.index("render") if render == "done" else keys.index("source")
    steps = []
    for i, k in enumerate(keys):
        st = "done" if i <= done_upto else "todo"
        if k == "source":
            st = source
        if k == "render" and render != "done":
            st = render
        steps.append({"key": k, "label": k, "status": st, "progress": 0.0, "detail": "", "sub": [], "started_at": None,
                      "ended_at": None, "error": None})
    return {"id": jid, "title": our_title, "source": {"gallery": gallery, "no": no, "title": src_title,
                                                      "url": f"https://gall.dcinside.com/board/view/?id={gallery}&no={no}"},
            "created_at": created_at, "status": status, "progress": 1.0, "options": {}, "steps": steps, "artifacts": {}, "log": []}


def write_job(root: Path, state: dict, origin_url: str | None = None) -> Path:
    d = root / state["id"]
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    if origin_url is not None:
        (d / "post").mkdir(exist_ok=True)
        (d / "post" / f"post_{state['source']['no']}.json").write_text(
            json.dumps({"no": state["source"]["no"], "origin_gallery": "잡담", "origin_url": origin_url}, ensure_ascii=False),
            encoding="utf-8")
    return d


@pytest.fixture(autouse=True)
def _fresh_cache():
    M.clear_cache()
    yield
    M.clear_cache()


# ---------------------------------------------------------------- 제목 정리
@pytest.mark.parametrize("raw,expected", [
    ("[가갤] 싱글벙글 동네 문구점에서 산 100원 연필.jpg", "동네문구점에서산100원연필"),
    ("[가갤] 안싱글벙글 야간 근무 첫날 후기", "야간근무첫날후기"),
    ("[가갤] 와들와들 이웃과 결혼 후 바뀐 사람...JPG", "이웃과결혼후바뀐사람"),
    ("[가갤] 훌쩍훌쩍 폐교를 지킨 마지막 학생이 받은 선물", "폐교를지킨마지막학생이받은선물"),
    ("[카연] 요즘 화제인 작가 vs 편집자 대결 ㅎㄷㄷ", "요즘화제인작가vs편집자대결"),
    ("[치갤] 판다 삼국지.manhwa", "판다삼국지"),
    ("블라인드) 택배 때문에 이사하면 어떨거같아?.jpg", "택배때문에이사하면어떨거같아"),
    ("[미갤] 헉ㅋㅋㅋㅋㅋㅋㅋㅋㅋㅋㅋㅋㅋㅋㅋㅋㅋ", "헉"),
    ("AI 그림ㅋㅋ라고 분노하는 사람들...jpg", "ai그림라고분노하는사람들"),
    ("[가갤] 싱글벙글 어떤 회사 주식 레버리지 근황...", "어떤회사주식레버리지"),
    ("(스압?)갑자기 생각나서 대충 적어보는 25시즌 결산", "갑자기생각나서대충적어보는25시즌결산"),
    ('"간판 모양이 이상한데 장난임?"', "간판모양이이상한데장난임"),
    ("3만 원짜리 중고 의자, 실제 상태 ㄷㄷ.jpg", "3만원짜리중고의자실제상태"),
    ("", ""),
    (None, ""),
])
def test_normalize_title(raw, expected):
    assert M.normalize_title(raw) == expected


def test_similarity_ignores_tags_suffixes_and_laughter():
    a = "[가갤] 싱글벙글 동네 문구점에서 산 100원 연필.jpg"
    assert M.similarity(a, "동네 문구점에서 산 100원 연필 ㅋㅋㅋ") == 1.0
    assert M.similarity(a, "[미갤] 동네 문구점에서 산 100원 연필...PNG") == 1.0
    # 전혀 다른 제목, 너무 짧은 제목, 빈 제목
    assert M.similarity(a, "오늘 동네 시장에 사람이 엄청 많네?") == 0.0
    assert M.similarity("빵집", "빵집 근황") == 0.0       # 정리하면 너무 짧아 비교하지 않음
    assert M.similarity("", "아무거나 긴 제목입니다") == 0.0


def test_similarity_threshold_on_real_wordings():
    """'원본 글 제목 ↔ 우리 영상 제목'(같은 이야기를 다르게 쓴 쌍) 중 겹치는 말이 뚜렷한 것은 기준 이상,
    제목이 전혀 다른 일반 글은 기준 미만."""
    same = [("[가상] 동네 빵집 오픈런 실제 대기 시간.jpg", "빵집 오픈런 대기 시간이 이렇게 길다고?"),
            ("블라인드) 택배 때문에 이사하면 어떨거같아?.jpg", "택배 때문에 이사까지 고민한다는 사연"),
            ("동네 목공방은 목재를 어디서 조달할까?", "목공방 목재는 어디서 올까?")]
    for a, b in same:
        assert M.similarity(a, b) >= M.SIMILAR_THRESHOLD, (a, b)
    diff = [("새로 산 자전거 배송이 취소된 사연", "인생이 허무한 진짜 이유"),
            ("동네 시장 상인들이 요즘 쓰는 결제 방법", "요즘 취업난 많이 심각하다네"),
            ("학교 교실의 창문은 항상 왼쪽인 이유", "동네 빵집이 아침에만 문을 여는 이유")]
    for a, b in diff:
        assert M.similarity(a, b) < M.SIMILAR_THRESHOLD, (a, b)


def test_origin_key_and_fmt_when():
    assert M.origin_key("https://gall.dcinside.com/board/view/?id=testgallery&no=2001") == ("testgallery", 2001)
    assert M.origin_key("https://gall.dcinside.com/mgallery/board/view/?id=x&no=5&page=2") == ("x", 5)
    assert M.origin_key("") is None and M.origin_key(None) is None and M.origin_key("https://example.com/") is None
    assert M.fmt_when("2026-10-18T12:38:45+09:00") == "10-18 12:38"
    assert M.fmt_when("이상한 값") == "이상한 값" and M.fmt_when(None) == ""


# ---------------------------------------------------------------- 색인
def build_fixture(tmp_path):
    root = tmp_path / "work"
    # 로컬: 같은 글로 job 3개(영상까지 / 영상까지 / 중간에 멈춤), 원본 주소가 있는 dcbest job, 글 가져오기에 실패한 job
    write_job(root, fake_state("20261017-190523_baseball_new13_2001", "baseball_new13", 2001, "[가상] 3만 원짜리 중고 의자, 실제 상태 ㄷㄷ.jpg",
                               "3만 원 중고 의자 실제 상태가 이렇다고?", "2026-10-17T19:05:23+09:00"))
    write_job(root, fake_state("20261017-195542_baseball_new13_2001", "baseball_new13", 2001, "[가상] 3만 원짜리 중고 의자, 실제 상태 ㄷㄷ.jpg",
                               "3만 원에 산 중고 의자, 실제 상태는 이랬어", "2026-10-17T19:55:42+09:00"))
    write_job(root, fake_state("20261017-201059_baseball_new13_2001", "baseball_new13", 2001, "[가상] 3만 원짜리 중고 의자, 실제 상태 ㄷㄷ.jpg",
                               "3만 원 중고 의자 실제로 받아 보니", "2026-10-17T20:10:59+09:00", render="todo", status="waiting"))
    write_job(root, fake_state("20261026-072157_dcbest_2002", "dcbest", 2002, "[가상] 새로 산 자전거 배송이 취소된 사연",
                               "자전거 배송 하루 앞두고 취소된 진짜 이유", "2026-10-26T07:21:57+09:00"),
              origin_url="https://gall.dcinside.com/board/view/?id=testgallery&no=2001")
    write_job(root, fake_state("20261027-100000_dcbest_999", "dcbest", 999, "가져오지 못한 글", "가져오지 못한 글",
                               "2026-10-27T10:00:00+09:00", render="todo", source="error", status="error"))
    # 원본 주소가 있는 로컬 job / 원본 주소를 모르는 로컬 job
    write_job(root, fake_state("20261019-215321_dcbest_2003", "dcbest", 2003, "[가상] 동네 목공방은 목재를 어디서 조달할까?",
                              "목공방 목재는 어디서 올까?", "2026-10-19T21:53:21+09:00"),
              origin_url="https://gall.dcinside.com/board/view/?id=testgallery&no=2004")
    write_job(root, fake_state("20261019-024130_dcbest_2005", "dcbest", 2005, "[가상] 폐교를 지킨 마지막 학생이 받은 선물",
                              "폐교를 지킨 마지막 학생의 선물", "2026-10-19T02:41:30+09:00"))
    return root


def test_build_index_local_jobs(tmp_path):
    root = build_fixture(tmp_path)
    idx = M.build_index(root)
    ids = [j.job_id for j in idx.jobs]
    assert len(idx) == 7 - 1                                         # 글 가져오기 실패 job 은 뺀다
    assert "20261027-100000_dcbest_999" not in ids
    assert ids == sorted(ids, key=lambda i: next(j.created_at for j in idx.jobs if j.job_id == i))     # 만든 시각 순
    post = idx.same_post("baseball_new13", 2001)
    assert [j.finished for j in post] == [True, True, False]         # 세 번째는 영상까지 못 감
    j = idx.same_post("dcbest", 2002)[0]
    assert j.origin == ("testgallery", 2001) and j.source_title == "[가상] 새로 산 자전거 배송이 취소된 사연" and j.our_title.startswith("자전거")
    on = idx.same_post("dcbest", 2003)[0]
    assert on.origin == ("testgallery", 2004)
    off = idx.same_post("dcbest", 2005)[0]
    assert off.origin_url == "" and off.our_title == "폐교를 지킨 마지막 학생의 선물"


def test_build_index_tolerates_junk(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    (root / "_archive.json").write_text("{깨진 json", encoding="utf-8")
    (root / "broken").mkdir()
    (root / "broken" / "state.json").write_text("not json", encoding="utf-8")
    (root / "wrongname").mkdir()                                                                               # 폴더 이름과 id 가 다름 → 건너뜀
    (root / "wrongname" / "state.json").write_text(json.dumps(fake_state("other_id", "dcbest", 1, "제목 제목 제목", "제목")), encoding="utf-8")
    write_job(root, {"id": "nosrc", "title": "x", "steps": []})                                                # source 없음 → 건너뜀
    write_job(root, fake_state("ok_job", "dcbest", 5, "정상적인 제목입니다", "정상 제목"))
    assert [j.job_id for j in M.build_index(root).jobs] == ["ok_job"]
    assert len(M.build_index(tmp_path / "없는 폴더")) == 0


def test_get_index_cache_follows_files(tmp_path):
    root = tmp_path / "work"
    write_job(root, fake_state("20261001-000000_dcbest_1", "dcbest", 1, "첫 번째 글 제목입니다", "첫 번째"))
    a = M.get_index(root)
    assert M.get_index(root) is a                                      # 파일이 그대로면 같은 색인
    write_job(root, fake_state("20261002-000000_dcbest_2", "dcbest", 2, "두 번째 글 제목입니다", "두 번째"))
    b = M.get_index(root)
    assert b is not a and len(b) == 2                                  # job 이 늘면 새로 만든다
    write_job(root, fake_state("20261003-000000_dcbest_3", "dcbest", 3, "세 번째 글 제목입니다", "세 번째"))
    c = M.get_index(root)
    assert c is not b and len(c) == 3                                  # 세 번째 로컬 job 도 반영
    st = fake_state("20261002-000000_dcbest_2", "dcbest", 2, "두 번째 글 제목입니다", "바뀐 영상 제목")
    write_job(root, st)
    assert M.get_index(root).same_post("dcbest", 2)[0].our_title == "바뀐 영상 제목"       # state.json 이 바뀌어도(내용 길이가 달라짐)


# ---------------------------------------------------------------- 견주기
def test_match_same_post_prefers_latest_and_counts(tmp_path):
    idx = M.build_index(build_fixture(tmp_path))
    m = idx.match("baseball_new13", 2001, "아무 제목")
    assert m["kind"] == "same_post" and m["count"] == 3 and m["score"] == 1.0
    assert m["job_id"].endswith("201059_baseball_new13_2001") and m["finished"] is False       # 가장 최근 job
    assert m["our_title"] == "3만 원 중고 의자 실제로 받아 보니" and m["created_at"].startswith("2026-10-17T20:10")
    assert idx.match("dcbest", 2005, "x")["finished"] is True
    # 같은 번호라도 갤러리가 다르면 같은 글이 아니다
    assert (idx.match("dcbest", 2001, "전혀 상관없는 제목이 들어 있는 글") or {}).get("kind") != "same_post"


def test_match_similar_and_none(tmp_path):
    idx = M.build_index(build_fixture(tmp_path))
    m = idx.match("dcbest", 3001, "[가갤] 싱글벙글 동네 목공방은 목재를 어디서 조달할까?.jpg")
    assert m["kind"] == "similar" and m["job_id"].endswith("dcbest_2003") and m["score"] >= M.SIMILAR_THRESHOLD and m["count"] == 1
    # 우리 영상 제목과 비슷한 새 글도 잡는다(원본 제목이 아니라 영상 제목 쪽과 겹침)
    m2 = idx.match("dcbest", 3002, "폐교를 지킨 마지막 학생의 선물 이야기")
    assert m2["kind"] == "similar" and m2["job_id"].endswith("dcbest_2005")
    assert idx.match("dcbest", 3003, "오늘 시장에 사람이 엄청 많네?") is None
    assert idx.match("dcbest", 3004, "짧음") is None


def test_match_same_original(tmp_path):
    idx = M.build_index(build_fixture(tmp_path))
    url = "https://gall.dcinside.com/board/view/?id=testgallery&no=2001"
    m = idx.match("dcbest", 555, "제목이 전혀 다른 글입니다 정말로", origin_url=url)
    assert m["kind"] == "same_original" and m["job_id"].endswith("dcbest_2002")
    assert idx.same_original("https://gall.dcinside.com/board/view/?id=nothing&no=1") == []
    # 원본 글 자체로 만든 job 도 같은 원본으로 본다
    ids = [j.job_id for j in idx.same_original("https://gall.dcinside.com/board/view/?id=baseball_new13&no=2001")]
    assert len(ids) == 3
    assert idx.match("dcbest", 556, "제목이 전혀 다른 글입니다 정말로") is None            # 원본 주소 없이는 같은 원본을 알 수 없다


def test_annotate_and_drop(tmp_path):
    idx = M.build_index(build_fixture(tmp_path))
    items = [{"no": 2002, "title": "[가상] 새로 산 자전거 배송이 취소된 사연"},
             {"no": 1, "title": "동네 목공방은 목재를 어디서 조달할까?"},
             {"no": 2, "title": "오늘 시장에 사람이 엄청 많네?"}]
    extra = M.annotate_items(idx, items, "dcbest")
    assert extra == {"made_count": 2, "made_same_count": 1}
    assert [i["made"]["kind"] if i["made"] else None for i in items] == ["same_post", "similar", None]
    assert [i["no"] for i in M.drop_made(items)] == [1, 2]            # 같은 글만 빠지고 비슷한 이야기는 남는다


def test_duplicate_message_names_jobs(tmp_path):
    idx = M.build_index(build_fixture(tmp_path))
    msg = M.duplicate_message(idx.same_post("baseball_new13", 2001))
    assert msg.startswith("이미 만든 글입니다 (3번)")
    assert "10-17 20:10 · 3만 원 중고 의자 실제로 받아 보니 (영상 안 끝남)" in msg and msg.index("20:10") < msg.index("19:55")      # 최신 순


def test_legacy_archive_and_external_paths_are_never_read(tmp_path, monkeypatch):
    root = tmp_path / "work"
    write_job(root, fake_state("local_job", "dcbest", 1, "로컬에서 만든 이야기", "로컬 영상"))
    external = tmp_path / "external_drive"
    outside_state = fake_state("outside_job", "dcbest", 2, "외부 보관 이야기", "외부 영상")
    write_job(external, outside_state, origin_url="https://example.invalid/private")
    legacy = root / "_archive.json"
    legacy.write_text(json.dumps({"jobs": [{"id": "outside_job", "state": outside_state,
                                           "path": str(external / "outside_job")}]}))
    reads = []
    read_text = Path.read_text

    def guarded_read(path, *args, **kwargs):
        if path == legacy or external in path.parents:
            reads.append(path)
            raise AssertionError("외부 보관 색인을 읽으면 안 됩니다")
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read)
    first = M.get_index(root)
    assert [j.job_id for j in first.jobs] == ["local_job"]
    legacy.write_text("legacy archive changed; still irrelevant")
    assert M.get_index(root) is first
    assert reads == []


def test_job_and_post_symlinks_do_not_import_external_history(tmp_path):
    root = tmp_path / "work"
    local = write_job(root, fake_state("local_job", "dcbest", 1, "로컬 이야기입니다", "로컬 영상"))
    external = tmp_path / "external_drive"
    outside = write_job(external, fake_state("outside_job", "dcbest", 2, "외부 이야기입니다", "외부 영상"),
                        origin_url="https://example.invalid/private")
    (root / "outside_job").symlink_to(outside, target_is_directory=True)
    (local / "post").symlink_to(outside / "post", target_is_directory=True)
    idx = M.build_index(root)
    assert [j.job_id for j in idx.jobs] == ["local_job"]
    assert idx.jobs[0].origin_url == ""
