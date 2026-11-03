import json

import pytest
from PIL import Image

from recap_shorts.images import arrange, captures, provider
from recap_shorts.script.pipeline import generate_script
from recap_shorts.script.refine import merge_refined
from tests.pipeline.fakes import DRAFT, FakeLLM, png, READ_TEXT



def test_generate_script_saves_draft_and_final(tmp_path):
    topic = {"source_text": READ_TEXT, "topic": "t"}
    llm = FakeLLM()
    events = []
    sc, chk = generate_script(topic, tmp_path, llm, on_stage=lambda *a: events.append(a))
    assert (tmp_path / "script_draft.json").is_file() and (tmp_path / "script.json").is_file()
    assert chk["refine"]["applied"] and len(chk["refine"]["changed"]) == 4
    assert [e[:2] for e in events] == [("draft", "start"), ("draft", "done"), ("refine", "start"), ("refine", "done")]
    sc2, chk2 = generate_script(topic, tmp_path / "b", FakeLLM(), refine=False)
    assert len(llm.calls) == 2 and "refine" not in chk2


def test_merge_refined_keeps_facts():
    draft = json.loads(json.dumps(DRAFT))
    ref = json.loads(json.dumps(DRAFT))
    ref["sentences"][0].update(text="바뀐 문장", evidence="지어낸 근거", image_hint="다른 힌트", emphasis="없는낱말")
    out, why = merge_refined(draft, ref)
    s = out["sentences"][0]
    assert s["text"] == "바뀐 문장" and s["evidence"] == DRAFT["sentences"][0]["evidence"]
    assert s["image_hint"] == DRAFT["sentences"][0]["image_hint"] and s["emphasis"] is None
    ref["sentences"].pop()
    assert merge_refined(draft, ref)[0] is None



def test_build_prompt_and_hints():
    p = provider.build_prompt("A factory floor.")
    assert p.startswith("Photorealistic") and "4:3" in p and "Absolutely no text" in p and "fictional ordinary person" in p
    assert provider.hints_to_prompts(["공장", "야구장"], text_fn=lambda q: {"prompts": ["factory", "stadium"]}) == ["factory", "stadium"]
    with pytest.raises(RuntimeError):
        provider.hints_to_prompts(["a", "b"], text_fn=lambda q: {"prompts": ["only one"]})


def test_generate_many_two_workers(tmp_path):
    import threading
    cur, peak, lock = [0], [0], threading.Lock()

    def gen(prompt, out, aspect):
        with lock:
            cur[0] += 1
            peak[0] = max(peak[0], cur[0])
        threading.Event().wait(0.05)
        with lock:
            cur[0] -= 1
        return {"ok": True, "file": str(out)}
    res = provider.generate_many([{"prompt": str(i), "out": tmp_path / f"{i}.png"} for i in range(6)], workers=5, gen=gen)
    assert len(res) == 6 and peak[0] == 2


def test_split_points_find_blank_band(tmp_path):
    im = Image.new("RGB", (500, 1800), (30, 30, 30))
    from PIL import ImageDraw
    d = ImageDraw.Draw(im)
    d.rectangle((0, 880, 500, 940), fill=(255, 255, 255))      # 가운데 근처 빈 띠(균일)
    pts = captures.split_points(im, 2)
    assert 860 <= pts[1] <= 960 and pts[0] == 0 and pts[-1] == 1800
    assert captures.piece_count(1000, 900) == 1 and captures.piece_count(600, 2000) == 3


def test_arrange_assign_no_overlap_and_reuse():
    scores = [[0.9, 0.1, 0.1], [0.8, 0.7, 0.1], [0.1, 0.2, 0.9]]
    plan = arrange.assign(scores, [1, 1, 1])
    assert sorted(j for p in plan for j in p) == [0, 1, 2]
    plan = arrange.assign(scores, [2, 2, 2])                       # 추가 칸은 맞는 그림이 없으면 비움 — 다시 쓰지 않음
    used = [j for p in plan for j in p]
    assert all(plan) and len(used) == len(set(used))
    plan = arrange.assign([[0.9], [0.8]], [1, 1])                   # 기본 칸이 그림보다 많을 때만 다시 씀
    assert plan == [[0], [0]]
    assert arrange.shots_for(5.0, True) == 1 and arrange.shots_for(4.4, False) == 3 and arrange.shots_for(0.5, False) == 1


def test_arrange_capture_first_ai_last_and_no_reuse():
    # 그림: 0,1 캡처(통째) · 2 문장 1용 잘라 낸 캡처 · 3,4 AI
    kinds = ["cap", "cap", "focus", "ai", "ai"]
    owners = [None, None, 1, None, None]
    scores = [[0.3, 0.2, 0.1, 0.9, 0.8],      # 훅: 칸 1개 — AI 도 가능
              [0.4, 0.3, 0.95, 0.9, 0.2],
              [0.2, 0.6, 0.1, 0.3, 0.9]]
    plan = arrange.assign(scores, [1, 3, 3], kinds, owners)
    used = [j for p in plan for j in p]
    assert len(used) == len(set(used))                                 # 같은 그림 두 번 없음
    for p in plan:
        ai = [k for k, j in enumerate(p) if kinds[j] == "ai"]
        assert p and len(ai) <= 1 and (not ai or ai[0] == len(p) - 1)  # AI 는 문장당 1장, 맨 끝
    assert 2 in plan[1] and 2 not in plan[2]                           # 잘라 낸 캡처는 제 문장에
    # AI 비율 상한: 0.2 이면 AI 컷이 전체의 20% 이하(캡처가 있으니까)
    plan = arrange.plan_with_ai_cap(scores, [1, 3, 3], kinds, owners, 0.2)
    total = sum(len(p) for p in plan)
    assert sum(1 for p in plan for j in p if kinds[j] == "ai") <= 0.2 * total


def test_heuristic_prefers_focus_and_dc_head():
    sents = [{"index": 0, "role": "hook", "hint": "영상 첫 화면"}, {"index": 1, "role": "body", "hint": "AI 장면: x / 캡처: 표"}]
    imgs = [{"file": "h", "kind": "cap", "role": "dc_head"}, {"file": "f", "kind": "cap", "role": "focus", "sentence": 1},
            {"file": "a", "kind": "ai", "sentence": 1}, {"file": "w", "kind": "cap"}]
    sc = arrange.heuristic_scores(sents, imgs)
    assert sc[0][0] == max(sc[0])                                      # 훅은 디시 글 머리
    assert sc[1][1] > sc[1][2] > sc[1][0]                              # 문장 1: 잘라 낸 캡처 > 그 문장 AI > 글 머리


def test_heuristic_drops_attachments_of_other_stories():
    # 한 글에 서로 다른 커뮤니티 글 두 개(+댓글)와 구인 공고를 묶어 놓은 경우. 대본은 두 번째 글(첨부 3·4)만 다루는데
    # 첫 번째 글과 공고 캡처가 빈 칸을 채우면 공고에 이름이 보이는 회사가 이 이야기의 주인공처럼 보인다.
    sents = [{"index": 0, "role": "hook", "hint": "영상 첫 화면"},
             {"index": 1, "role": "body", "hint": "AI 장면: x / 캡처: 첨부 그림 3번 '단골' 줄"},
             {"index": 2, "role": "body", "hint": "AI 장면: y"}]
    atts = [{"n": 1, "kind": "SNS 글 캡처", "summary": "주 6일 2교대 월 250만 원 물류 구인 공고를 보고 지원을 망설인다는 글"},
            {"n": 2, "kind": "커뮤니티 댓글 캡처", "summary": "카페 마감 알바 실수령 이야기"},
            {"n": 3, "kind": "SNS 글 캡처", "summary": "새로 연 빵집 사장님이 첫 주 만에 단골손님 이름을 다 외워 불러 줬다는 커뮤니티 글"},
            {"n": 4, "kind": "댓글 캡처", "summary": "단골 이름 외우기를 두고 나눈 댓글"},
            {"n": 5, "kind": "구인공고 캡처", "summary": "채용 사이트 생산직 채용공고 상세"},
            {"n": 9, "kind": "디시 댓글 캡처", "dc_comments": True, "summary": "이 글에 달린 디시 댓글"}]
    imgs = [{"file": "c01", "kind": "cap", "attachment": 1}, {"file": "c02", "kind": "cap", "attachment": 2},
            {"file": "c03", "kind": "cap", "attachment": 3}, {"file": "c04", "kind": "cap", "attachment": 4},
            {"file": "c05", "kind": "cap", "attachment": 5},
            {"file": "c04_s2", "kind": "cap", "role": "focus", "sentence": 2, "attachment": 4},
            {"file": "a2", "kind": "ai", "sentence": 2}]
    assert arrange.sentence_attachments(sents, imgs) == [set(), {"3"}, {"4"}]
    assert arrange.unrelated_attachments(sents, imgs, atts) == {"1", "2", "5"}   # 디시 댓글은 늘 남김
    assert arrange.off_script(sents, imgs, atts) == {0, 1, 4}
    sc = arrange.heuristic_scores(sents, imgs, atts)
    assert all(row[j] == 0.0 for row in sc for j in (0, 1, 4))         # 다른 이야기 첨부는 어느 문장에도 0
    assert sc[1][2] > sc[1][3] and sc[2][3] > sc[2][2]                 # 문장이 말하는 첨부의 통째 캡처가 먼저
    clef = [[0.9] * len(imgs) for _ in sents]
    blended = arrange.blend(sc, clef, arrange.off_script(sents, imgs, atts))
    assert all(row[j] == 0.0 for row in blended for j in (0, 1, 4))    # Clef 를 섞어도 0
    plan = arrange.assign(sc, [1, 3, 3], [arrange.kind_of(e) for e in imgs])
    assert not {0, 1, 4} & {j for p in plan for j in p}                # 빈 칸이 남아도 끌어오지 않음


def test_heuristic_keeps_comments_and_similar_attachments_of_the_same_story():
    sents = [{"index": 1, "role": "body", "hint": "캡처: 첨부 그림 1의 '십 년째' 줄"}]
    atts = [{"n": 1, "kind": "블라인드 글 캡처", "summary": "세탁소 주인이 우산을 맡기고 찾아가지 않은 손님을 십 년째 기다리는 글"},
            {"n": 2, "kind": "블라인드 댓글 캡처", "summary": "전혀 다른 말투의 댓글들"},           # 글자는 안 겹쳐도 댓글은 앞 글에 붙음
            {"n": 3, "kind": "영상 캡처", "summary": "세탁소 주인이 우산을 맡기고 찾아가지 않은 손님을 기다리는 장면"},   # 글자가 많이 겹치면 같은 이야기
            {"n": 4, "kind": "일러스트", "summary": "한옥 골목에서 호외 신문을 든 청년"}]
    imgs = [{"file": f"c0{n}", "kind": "cap", "attachment": n} for n in (1, 2, 3, 4)]
    assert arrange.unrelated_attachments(sents, imgs, atts) == {"4"}


def test_heuristic_keeps_author_photos_with_little_text():
    """글쓴이가 올린 도예 공방 사진 8장(text 0~16자, kind "사진")과 커뮤니티 댓글 캡처.
    사진에는 글자가 없어 대본과 겹침을 잴 수 없는데, 다른 이야기로 빼 버리면 AI 가 만든 사진이 글쓴이 사진 자리를
    채운다(시청자에게 글쓴이 사진처럼 보임). 사진은 이야기 규칙에서 빼지 않고, 글자 많은 캡처 규칙은 그대로 둔다."""
    sents = [{"index": 0, "role": "hook", "hint": "영상 첫 화면"},
             {"index": 1, "role": "body", "hint": "AI 장면: 물레 / 캡처: 본문 '흙의 결'"},
             {"index": 2, "role": "body", "hint": "AI 장면: 가마 / 캡처: 커뮤니티 댓글 '22짤'"},
             {"index": 3, "role": "body", "hint": "AI 장면: 작업대"}]
    atts = [{"n": 1, "kind": "사진", "text": "456 산길 Trail",
             "summary": "푸른 하늘 아래 우뚝 솟은 거대한 간판과 도로 표지판이 보이는 풍경"}]
    atts += [{"n": n, "kind": "사진", "text": "",
              "summary": s} for n, s in zip(range(2, 9), [
                  "작업대 위에서 물레를 돌리며 흙을 빚는 두 손의 모습", "선반에 가지런히 놓인 물레와 도구들",
                  "가마 앞에서 완성된 도자기를 들어 보이는 사람의 모습", "푸른 유약을 바른 접시 여러 장의 모습",
                  "작업대 위에 흩어진 점토 조각과 조각칼", "가마 안에서 붉게 달아오른 도자기 그릇",
                  "손에 든 작은 주발의 안쪽 면 가까이 찍은 사진"])]
    atts.append({"n": 9, "kind": "커뮤니티 댓글 캡처", "dc_comments": True, "text": "직접 가 보니 생각보다 조용했어! 흙 냄새가 좋았어.",
                 "summary": "이 글에 달린 커뮤니티 댓글"})
    imgs = [{"file": f"c{n:02d}", "kind": "cap", "attachment": n} for n in range(1, 10)]
    imgs += [{"file": "c09_s2", "kind": "cap", "role": "focus", "sentence": 2, "attachment": 9},
             {"file": "ai1", "kind": "ai", "sentence": 1}, {"file": "head", "kind": "cap", "role": "dc_head"}]
    assert arrange.unrelated_attachments(sents, imgs, atts) == set()
    assert arrange.off_script(sents, imgs, atts) == set()
    # 사진은 점수를 그대로 받아 실제 사진이 배치되고 AI 가 밀린다(대본이 사진을 말하지 않아도)
    sc = arrange.heuristic_scores(sents, imgs, atts)
    plan = arrange.assign(sc, [1, 3, 3, 2], [arrange.kind_of(e) for e in imgs])
    used = [j for p in plan for j in p]
    photos = {imgs[j]["file"] for j in used if str(imgs[j].get("attachment", "")).isdigit() and int(imgs[j]["attachment"]) <= 8}
    assert len(photos) >= 5 and sum(1 for j in used if imgs[j]["kind"] == "ai") == 0


def test_photo_rule_does_not_keep_text_heavy_other_story():
    """글자 많은 첨부(구인 공고)는 그대로 빠져야 한다 — 다른 이야기 글 꼴. 사진 예외는 사진·짤이면서 글자가 짧을 때만."""
    sents = [{"index": 1, "role": "body", "hint": "캡처: 첨부 그림 3번 '단골' 줄"}]
    long_text = ("채용 사이트 통합 검색 전체메뉴 채용정보 상세 기업명 (주)가온물산 사업자등록번호 123-45-67890 "
                 "근무지 주소 경기도 성남시 판교동 판교로 12번길 34 모집부문 생산관리 직무내용 자재 입출고 및 재고 관리 "
                 "근무형태 주 5일 통상시급 12,000원 4대보험 가입 퇴직금 별도")
    atts = [{"n": 1, "kind": "구인공고 캡처", "text": long_text, "summary": "생산직 채용공고 상세"},
            {"n": 2, "kind": "사진", "text": "상가 앞에서 찍은 단체 사진. 이날 새로 연 빵집 사장님이 단골 이름을 외웠다는 소문이 돌았다. 상가 주차장은 뒷골목 공영주차장, 번영회 회의는 매달 첫째 주 월요일",
             "summary": "상가 앞"},
            {"n": 3, "kind": "SNS 글 캡처", "summary": "새로 연 빵집 사장님이 첫 주 만에 단골손님 이름을 다 외웠다는 커뮤니티 글"}]
    imgs = [{"file": "c1", "kind": "cap", "attachment": 1}, {"file": "c2", "kind": "cap", "attachment": 2},
            {"file": "c3", "kind": "cap", "attachment": 3}]
    assert arrange.unrelated_attachments(sents, imgs, atts) == {"1", "2"}
    assert arrange.off_script(sents, imgs, atts) == {0, 1}
    # kind 가 "사진"이어도 글자가 길면 사진으로 보지 않는다(글자로 이야기를 가릴 수 있으므로)
    assert arrange._is_photo(atts[0]) is False and arrange._is_photo(atts[1]) is False


def test_heuristic_keeps_all_attachments_when_unsure():
    imgs = [{"file": "c01", "kind": "cap", "attachment": 1}, {"file": "c02", "kind": "cap", "attachment": 2}]
    atts = [{"n": 1, "kind": "글 캡처", "summary": "가"}, {"n": 2, "kind": "공고 캡처", "summary": "나"}]
    named = [{"index": 1, "role": "body", "hint": "캡처: 첨부 그림 1"}]
    unnamed = [{"index": 1, "role": "body", "hint": "캡처: 게시글 본문"}]
    assert arrange.off_script(unnamed, imgs, atts) == set()            # 대본이 첨부 번호를 안 적었으면 모두 씀
    assert arrange.off_script(named, imgs, None) == set()              # 첨부 목록이 없어도 모두 씀
    assert arrange.off_script(named, imgs, atts) == {1}
    assert all(v > 0 for v in arrange.heuristic_scores(unnamed, imgs, atts)[0])


def test_regions_crop_for_widens_thin_rows():
    from recap_shorts.images import regions
    crop, widened = regions.crop_for((100, 500, 600, 540), (800, 1000))
    assert widened and (crop[3] - crop[1]) >= (crop[2] - crop[0]) / 3.2 - 1
    assert crop[1] < 500 and crop[3] > 540
    crop, widened = regions.crop_for((10, 10, 300, 200), (800, 1000))
    assert not widened


def test_regions_locate_converts_and_splits_tall(tmp_path):
    from PIL import Image
    from recap_shorts.images import regions
    p = tmp_path / "tall.png"
    Image.new("RGB", (500, 1500), (255, 255, 255)).save(p)
    calls = []

    def read(images, instruction):
        calls.append(images[0].size)
        return {"parts": [{"q": 0, "box": [100, 0, 200, 1000]}], "private": [{"what": "아이디", "box": [0, 0, 50, 100]}]}
    r = regions.locate(p, ["댓글"], read=read)
    assert len(calls) == 3                                             # 1500/500 → 3 조각
    assert r["parts"][0][0] == 0 and r["parts"][0][2] == 500           # 첫 조각에서 찾은 것(가로 0~1000 → 0~500)
    assert len(r["private"]) == 3 and r["private"][1][1] >= 400        # 조각마다 가릴 곳, 아래 조각은 y 가 밀림


def test_regions_parse_answer_repairs_missing_box_key():
    from recap_shorts.images.regions import parse_answer
    bad = '{"parts": [{"q": 0, "box": [1, 2, 3, 4]}, {"q": 1, null}], "private": [{"what": "아이디", "box": [5, 6, 7, 8]},]}'
    r = parse_answer(bad)
    assert r["parts"][1] == {"q": 1, "box": None} and len(r["private"]) == 1
    assert parse_answer('```json\n{"parts": [], "private": []}\n```') == {"parts": [], "private": []}
    assert parse_answer({"parts": []}) == {"parts": []}


def test_text_strip_reflows_lines_in_order(tmp_path):
    from PIL import Image, ImageDraw
    import numpy as np
    from recap_shorts.images import regions
    p = tmp_path / "two.png"
    im = Image.new("RGB", (1600, 120), (255, 255, 255))
    d = ImageDraw.Draw(im)
    for x in range(40, 1560, 60):                     # 1줄: 길다(빈칸 있는 글자 덩어리들)
        d.rectangle((x, 20, x + 40, 40), fill=(0, 0, 0))
    d.rectangle((40, 70, 300, 90), fill=(200, 0, 0))   # 2줄: 짧고 빨강(순서 확인용)
    im.save(p)
    out = regions.text_strip(p, (0, 0, 1600, 120), [], tmp_path / "s.png")
    st = Image.open(out)
    assert st.width <= regions.WRAP_WIDTH + 2 * 28 + 1                  # 줄을 바꿔 폭이 줄어듦
    a = np.asarray(st)
    red_rows = np.nonzero(((a[:, :, 0] > 150) & (a[:, :, 1] < 80)).any(axis=1))[0]
    black_rows = np.nonzero((a.sum(axis=2) < 100).any(axis=1))[0]
    assert red_rows.min() > black_rows.max()                            # 2줄은 1줄 조각들보다 아래(순서 유지)
    assert len(regions.line_spans(np.asarray(Image.open(p).convert("L")) < regions.INK)) == 2


def test_post_body_attachment_only_for_text_posts(tmp_path):
    """그림 없이 글만 있는 글: 본문 캡처를 첨부 0번으로 잘라 쓴다(블라인드 parts.body, 디시 머리~추천 상자 사이).
    첨부 그림이 있으면 본문 캡처 속 그림과 겹치므로 쓰지 않는다."""
    from recap_shorts import pipeline as P
    (tmp_path / "post").mkdir()
    Image.new("RGB", (400, 900), "white").save(tmp_path / "post" / "screenshot.png")
    comments = {"n": 1, "file": "post/comments.png", "kind": "블라인드 댓글 캡처", "dc_comments": True}
    parts = tmp_path / "post" / "screenshot_parts.json"

    parts.write_text(json.dumps({"head": [0, 0, 400, 120], "body": [0, 120, 400, 520]}), encoding="utf-8")
    a = P.post_body_attachment(tmp_path, [comments])
    assert a["n"] == 0 and a["file"] == "post/body.png" and a["body"] is True
    assert Image.open(tmp_path / "post" / "body.png").size == (400, 400)
    assert P.post_body_attachment(tmp_path, [comments, {"n": 2, "file": "post/a.png", "kind": "사진"}]) is None

    parts.write_text(json.dumps({"head": [10, 0, 390, 100], "recommend": [100, 700, 300, 760]}), encoding="utf-8")
    P.post_body_attachment(tmp_path, [])
    assert Image.open(tmp_path / "post" / "body.png").size == (380, 600)    # 디시: 머리 아래 ~ 추천 상자 위

    parts.write_text(json.dumps({"head": [0, 0, 400, 120], "body": [0, 120, 400, 140]}), encoding="utf-8")
    assert P.post_body_attachment(tmp_path, []) is None                      # 본문이 너무 낮음
    parts.write_text(json.dumps({"head": [0, 0, 400, 120]}), encoding="utf-8")
    assert P.post_body_attachment(tmp_path, []) is None                      # 본문 상자를 모름
