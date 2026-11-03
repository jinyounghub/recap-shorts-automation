import json
from pathlib import Path

import pytest

from recap_shorts import pipeline as P
from tests.pipeline.fakes import DRAFT, FakeDeps, FakeLLM

SRC = {"gallery": "baseball_new13", "no": 2101, "url": "https://gall.dcinside.com/board/view/?id=baseball_new13&no=2101",
       "minor": False}


def make(tmp_path, deps=None, options=None):
    store = P.JobStore(tmp_path)
    runner = P.Runner(store, deps or FakeDeps())
    runner.start()
    job = store.create(dict(SRC), options)
    return store, runner, job


def go(runner, job):
    runner.submit(job)
    assert runner.wait_idle(60)


def statuses(job):
    return {s["key"]: s["status"] for s in job.state["steps"]}


def test_full_flow_waits_at_confirm_steps(tmp_path):
    deps = FakeDeps()
    store, runner, job = make(tmp_path, deps)
    go(runner, job)
    assert job.state["status"] == "waiting"
    assert statuses(job)["source"] == "done" and statuses(job)["read"] == "waiting"
    assert deps.calls["get_post"] == 1
    topic = json.loads((job.dir / "topic.json").read_text(encoding="utf-8"))
    assert "[게시글 1 첨부 그림 1: 팬이 만든 비교표(확인 안 된 자료)]" in topic["source_text"]
    assert "병신" not in topic["source_text"]                       # 욕설 낱말 제거

    expected = [("script", "waiting"), ("scenes", "waiting"), ("review", "waiting")]
    for key, st in expected:
        P.continue_job(runner, job)
        assert runner.wait_idle(60)
        assert statuses(job)[key] == st, (key, statuses(job), job.state["log"][-3:])
        assert job.state["status"] == "waiting"
    # review 승인
    P.continue_job(runner, job)
    assert runner.wait_idle(60)
    assert job.state["status"] == "done" and all(v == "done" for v in statuses(job).values())
    d = job.dir
    for f in ("script_final/script.json", "script_final/script_draft.json", "voice/voice.wav", "assets/assets.json",
              "shot_plan.json", "timeline.json", "out/final.mp4", "out/description.txt", "out/review.json"):
        assert (d / f).is_file(), f
    # 다듬기 전/후가 다르고, 사실 필드는 그대로
    draft = json.loads((d / "script_final/script_draft.json").read_text(encoding="utf-8"))
    final = json.loads((d / "script_final/script.json").read_text(encoding="utf-8"))
    assert draft["sentences"][0]["text"] != final["sentences"][0]["text"]
    assert [s["evidence"] for s in draft["sentences"]] == [s["evidence"] for s in final["sentences"]]
    desc = (d / "out/description.txt").read_text(encoding="utf-8")
    assert "로컬 모델" in desc and "AI로 생성한 이미지" in desc and "팬이 만든" in desc
    assert "Gemini TTS" not in desc
    assert "gall.dcinside.com" in desc
    # 화면: 출처 줄 없음, 배경음악 없음
    tl = json.loads((d / "timeline.json").read_text(encoding="utf-8"))
    assert tl["audio"]["bgm"] is None
    arts = job.state["artifacts"]
    assert arts["video"] == "out/final.mp4" and arts["description"] == "out/description.txt"
    assert job.state["progress"] == 1.0


def test_auto_steps_skip_waiting(tmp_path):
    store, runner, job = make(tmp_path, options={"auto_topic": True, "auto_steps": ["script"]})
    go(runner, job)
    assert statuses(job)["read"] == "done" and statuses(job)["script"] == "done"
    assert statuses(job)["scenes"] == "waiting"


def test_error_then_resume_skips_done_steps(tmp_path):
    deps = FakeDeps()
    deps.fail_once.add("make_voice")
    store, runner, job = make(tmp_path, deps, {"auto_steps": ["read", "script"]})
    go(runner, job)
    st = statuses(job)
    assert job.state["status"] == "error" and st["voice"] == "error" and st["script"] == "done"
    assert "일부러 실패" in job.step("voice")["error"]
    P.continue_job(runner, job)
    assert runner.wait_idle(60)
    assert statuses(job)["voice"] == "done" and statuses(job)["scenes"] == "waiting"
    assert deps.calls["get_post"] == 1 and deps.calls["make_voice"] == 2   # 앞 단계는 다시 하지 않음
    assert len(deps.llm.calls) == 2                                       # 초안 + 다듬기, 재개 때 다시 부르지 않음


def test_refine_failure_falls_back_to_draft(tmp_path):
    deps = FakeDeps(llm=FakeLLM(fail_refine=True))
    store, runner, job = make(tmp_path, deps, {"auto_steps": ["read"]})
    go(runner, job)
    check = json.loads((job.dir / "script_final/script_check.json").read_text(encoding="utf-8"))
    assert check["refine"]["applied"] is False and "다듬기 호출 실패" in check["refine"]["error"]
    final = json.loads((job.dir / "script_final/script.json").read_text(encoding="utf-8"))
    assert final["sentences"][0]["text"] == DRAFT["sentences"][0]["text"]
    assert job.step("script")["sub"][1]["status"] == "error"


def test_refine_wrong_sentence_count_is_discarded(tmp_path):
    deps = FakeDeps(llm=FakeLLM(refine_texts=["하나만"]))     # zip → 문장 1개만 바뀌어 나머지는 초안 → 개수는 같음
    store, runner, job = make(tmp_path, deps, {"auto_steps": ["read"]})
    go(runner, job)
    final = json.loads((job.dir / "script_final/script.json").read_text(encoding="utf-8"))
    assert final["sentences"][0]["text"] == "하나만" and len(final["sentences"]) == len(DRAFT["sentences"])


def test_reuse_and_gen_limit(tmp_path):
    reuse = tmp_path / "reuse"
    for i in range(3):
        (reuse).mkdir(exist_ok=True)
        from tests.pipeline.fakes import png
        png(reuse / f"r{i}.png", (300, 200))
    deps = FakeDeps()
    store, runner, job = make(tmp_path / "w", deps, {"auto_steps": ["read", "script"],
                                                     "reuse_images_from": str(reuse), "image_gen_limit": 1})
    go(runner, job)
    assert statuses(job)["scenes"] == "waiting"
    assert deps.calls["generate_image"] == 1
    ents = json.loads((job.dir / "assets/assets.json").read_text(encoding="utf-8"))
    ai = [e for e in ents if e["kind"] == "ai"]
    st = [e["status"] for e in ai]
    # 캡처 힌트 문장에도 AI 그림을 만든다(wants_ai) — 만든 1장 + 나머지는 재사용
    assert st.count("ok") == 1 and len(st) >= 4 and all(x in ("ok", "reused") for x in st)
    assert any(e["kind"] == "cap" for e in ents)


def test_image_generation_off_uses_captures_only(tmp_path):
    deps = FakeDeps(image_up=False)
    store, runner, job = make(tmp_path, deps, {"auto_steps": ["read", "script"]})
    go(runner, job)
    ents = json.loads((job.dir / "assets/assets.json").read_text(encoding="utf-8"))
    assert all(e["kind"] == "cap" for e in ents)
    assert {x["label"]: x for x in job.step("assets")["sub"]}["AI 이미지"]["status"] == "done"           # 명시적으로 끈 생성은 오류가 아님
    assert statuses(job)["scenes"] == "waiting"                        # 다른 단계는 계속


def test_tall_attachment_split_into_pieces(tmp_path):
    store, runner, job = make(tmp_path, options={"auto_steps": ["read", "script"]})
    go(runner, job)
    ents = json.loads((job.dir / "assets/assets.json").read_text(encoding="utf-8"))
    pieces = [e for e in ents if e["kind"] == "cap" and e.get("attachment") == 2 and not e.get("role")]
    assert 2 <= len(pieces) <= 3
    assert all(e["needs_review"] for e in pieces)                       # 가릴 곳을 못 찾았고 Clef 없음 → 사람 확인 표시
    first = [e for e in ents if e["kind"] == "cap" and e.get("attachment") == 1]
    assert first and all(not e["needs_review"] and e["blur_boxes"] for e in first)   # 자동 가림 → 확인 표시 없음
    focus = [e for e in ents if e.get("role") == "focus"]
    assert focus and all(e["sentence"] == 2 and e["file"].endswith("_s02.png") for e in focus)   # 문장 2 힌트의 '캡처:' 부분
    assert any(e.get("role") == "dc_head" for e in ents)                 # 디시 글 머리 카드
    assert not any(e["file"].startswith("assets/cap/c03") and not e.get("role") for e in ents)   # 디시 댓글은 통째로 안 씀


def test_clef_matrix_drives_placement(tmp_path):
    deps = FakeDeps()

    def matrix(sentences, images):          # 모든 문장이 마지막 그림을 가장 좋아함 → 그래도 겹치지 않게 배치
        return [[1.0 if j == len(images) - 1 else 0.1 for j in range(len(images))] for _ in sentences]
    deps.clef_matrix = matrix
    store, runner, job = make(tmp_path, deps, {"auto_steps": ["read", "script"], "clef_scene_fit": True})
    go(runner, job)
    doc = json.loads((job.dir / "shot_plan.json").read_text(encoding="utf-8"))
    assert doc["scored_by"] == "clef"
    files = [f for fs in doc["plan"].values() for f in fs]
    assert len(set(files)) == len(files)                                # 같은 그림을 두 번 쓰지 않음(칸 수를 줄임)
    for fs in doc["plan"].values():                                     # AI 는 문장 끝에 1장까지 — 캡처가 먼저
        ai = [k for k, f in enumerate(fs) if f.startswith("assets/ai/")]
        assert len(ai) <= 1 and (not ai or ai[0] == len(fs) - 1)
    assert doc["plan"]["0"] == ["assets/cap/dc_head.png"]               # 훅은 디시 글 머리


def test_each_sentence_has_images_and_counts(tmp_path):
    store, runner, job = make(tmp_path, options={"auto_steps": ["read", "script"]})
    go(runner, job)
    doc = json.loads((job.dir / "shot_plan.json").read_text(encoding="utf-8"))
    assert list(doc["plan"]) == [str(i) for i in range(len(doc["plan"]))]
    assert len(doc["plan"]["0"]) == 1                                   # 훅은 한 장
    assert all(1 <= len(v) <= 3 for v in doc["plan"].values())


def test_state_json_shape_and_reload(tmp_path):
    store, runner, job = make(tmp_path, options={"auto_steps": ["read", "script"]})
    go(runner, job)
    st = json.loads((job.dir / "state.json").read_text(encoding="utf-8"))
    assert [s["key"] for s in st["steps"]] == P.STEP_KEYS
    for s in st["steps"]:
        assert set(s) >= {"key", "label", "status", "progress", "detail", "sub", "started_at", "ended_at", "error"}
    assert set(st) >= {"id", "title", "source", "created_at", "status", "steps", "artifacts", "log"}
    assert 0 <= st["progress"] <= 1 and st["log"] and set(st["log"][0]) == {"t", "msg"}
    # 서버를 다시 켜면 상태를 다시 읽고, 도중에 끊긴 running 은 error 로
    st["status"] = "running"
    st["steps"][3].update(status="running")
    (job.dir / "state.json").write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    store2 = P.JobStore(tmp_path)
    j2 = store2.get(job.id)
    assert j2.state["status"] == "error" and j2.step("voice")["status"] == "error"


def test_description_without_ai_images(tmp_path):
    deps = FakeDeps(image_up=False)
    store, runner, job = make(tmp_path, deps, {"auto_steps": ["read", "script", "scenes", "review"]})
    go(runner, job)
    assert job.state["status"] == "done"
    desc = (job.dir / "out/description.txt").read_text(encoding="utf-8")
    assert "AI로 생성한 이미지" not in desc and "AI 음성" in desc


def test_description_lists_shown_memes(tmp_path):
    """영상에 실제로 나온 짤만 [짤] 에 이름·출처를 적는다(출처는 영상 설명에). 채널 핸들은 쓰지 않는다."""
    deps = FakeDeps(image_up=False)
    store, runner, job = make(tmp_path, deps, {"auto_steps": ["read", "script", "scenes", "review"]})
    go(runner, job)
    assert "[짤]" not in (job.dir / "out/description.txt").read_text(encoding="utf-8")
    lib = tmp_path / "memes_x"
    lib.mkdir()
    items = [{"id": "m_shrug", "file": "m_shrug.png", "name": "물음표 으쓱 페페", "source_site": "예시 사이트 @somechannel",
              "source_url": "https://example.com/shrug", "license": "unknown (캐릭터 권리 Matt Furie)"},
             {"id": "m_fist", "file": "m_fist.png", "name": "주먹 페페", "source_site": "wallpapers.com",
              "source_url": "https://www.youtube.com/@somechannel/shorts", "license": "personal use only"},
             {"id": "m_unused", "file": "m_unused.png", "name": "안 나온 짤", "source_site": "x"}]
    (lib / "manifest.json").write_text(json.dumps({"version": 1, "items": items}, ensure_ascii=False), encoding="utf-8")
    tl = json.loads((job.dir / "timeline.json").read_text(encoding="utf-8"))
    tl["shots"] += [{"file": str(lib / "m_shrug.png"), "kind": "meme", "meme_id": "m_shrug", "start": 5.0, "end": 6.2},
                    {"file": str(lib / "m_shrug.png"), "kind": "meme", "meme_id": "m_shrug", "start": 20.0, "end": 21.2}]
    tl["overlays"] = [{"kind": "sticker", "file": str(lib / "m_fist.png"), "meme_id": "m_fist", "start": 9.0, "end": 10.0}]
    (job.dir / "timeline.json").write_text(json.dumps(tl, ensure_ascii=False), encoding="utf-8")
    desc = P.make_description(job)
    block = desc.split("[짤]\n", 1)[1].split("\n\n", 1)[0].splitlines()
    assert block == ["- 물음표 으쓱 페페 — 출처 예시 사이트 https://example.com/shrug · 캐릭터 원작 Matt Furie",
                     "- 주먹 페페 — 출처 wallpapers.com · 라이선스 personal use only"]
    assert "@" not in desc and "youtube.com" not in desc and "안 나온 짤" not in desc
    # 출처가 없는 짤은 '사용자 제공(…)' 같은 내부 기록을 공개 설명에 쓰지 않는다
    items[0].update(source_site="사용자 제공(파일로만 받음)", source_url="")
    (lib / "manifest.json").write_text(json.dumps({"version": 1, "items": items}, ensure_ascii=False), encoding="utf-8")
    desc = P.make_description(job)
    assert "- 물음표 으쓱 페페 — 캐릭터 원작 Matt Furie" in desc and "사용자 제공" not in desc and "unknown" not in desc


def test_clean_text():
    assert P.clean_text("대단하다!! 시발") == "대단하다!!"


def test_unblurred_risky_captures_are_not_auto_placed(tmp_path):
    """아이디가 있을 수 있는 첨부(has_id_text)에서 가릴 곳을 하나도 못 찾으면 그 카드는 자동 배치에 안 들어간다."""
    class NoBlurDeps(FakeDeps):
        def read_image(self, images, instruction):
            r = super().read_image(images, instruction)
            if isinstance(r, dict) and "kind" in r:
                r = {**r, "has_id_text": True}
            return r

        def locate_regions(self, path, wants):
            r = super().locate_regions(path, wants)
            return {**r, "private": []}
    store, runner, job = make(tmp_path, NoBlurDeps(), {"auto_steps": ["read", "script"]})
    go(runner, job)
    ents = json.loads((job.dir / "assets/assets.json").read_text(encoding="utf-8"))
    risky = {e["file"] for e in ents if e.get("unblurred_risk")}
    assert risky and all(e["needs_review"] for e in ents if e["file"] in risky)
    plan = json.loads((job.dir / "shot_plan.json").read_text(encoding="utf-8"))["plan"]
    assert not risky & {f for fs in plan.values() for f in fs}


def test_clef_scene_fit_is_off_by_default(tmp_path):
    """장면 배치의 Clef 적합도는 기본으로 끔(효과 거의 없고 뉴런만 씀) — 규칙으로 배치."""
    deps = FakeDeps()
    called = []
    deps.clef_matrix = lambda sentences, images: called.append(1) or [[0.5] * len(images) for _ in sentences]
    store, runner, job = make(tmp_path, deps, {"auto_steps": ["read", "script"]})
    go(runner, job)
    doc = json.loads((job.dir / "shot_plan.json").read_text(encoding="utf-8"))
    assert not called and doc["scored_by"] == "heuristic"


def test_render_step_upload_sound_note(tmp_path):
    """배경음악은 영상에 굽지 않고 올릴 때 넣는다(bgm_mode upload_note): 렌더가 안내 파일을 썼으면 산출물로 올리고,
    다시 렌더할 때 지난 안내가 남지 않게 먼저 지운다(이번 렌더에 요청이 없으면 아무 파일도 없어야 한다)."""
    class NoteDeps(FakeDeps):
        write_note = True

        def render(self, timeline, out_mp4, progress=None):
            res = super().render(timeline, out_mp4, progress)
            if self.write_note:
                note = Path(out_mp4).with_name("upload_sound.txt")
                note.write_text("[업로드할 때 넣을 배경음악]\n", encoding="utf-8")
                res["upload_sound"] = str(note)
            return res

    deps = NoteDeps(image_up=False)
    store, runner, job = make(tmp_path, deps, {"auto_steps": ["read", "script", "scenes", "review"]})
    go(runner, job)
    assert (job.dir / "out/upload_sound.txt").is_file()
    assert job.state["artifacts"]["upload_sound"] == "out/upload_sound.txt"

    deps.write_note = False                                   # 이번엔 배경음악 요청이 없다
    P.step_render(P.Ctx(job, "render", deps))
    assert not (job.dir / "out/upload_sound.txt").exists() and "upload_sound" not in job.state["artifacts"]
    job.artifact("upload_sound", "out/upload_sound.txt")
    job.reset_from("render")
    assert "upload_sound" not in job.state["artifacts"] and "video" not in job.state["artifacts"]


def _review_json(tmp_path, draft):
    deps = FakeDeps(llm=FakeLLM(draft=draft))
    store, runner, job = make(tmp_path, deps)
    go(runner, job)
    for _ in range(3):                                          # 자료 읽기 -> 대본 -> 장면 -> 검수 대기
        P.continue_job(runner, job)
        assert runner.wait_idle(60)
    assert statuses(job)["review"] == "waiting"
    return json.loads((job.dir / "out/review.json").read_text(encoding="utf-8"))


def test_review_warns_when_script_has_no_own_take(tmp_path):
    """F9: 문장이 모두 글 내용을 다시 전하는 것(근거 있음)이면 검수 화면의 대본 경고(script_flags)에 해석 없음이 뜬다."""
    ev = "상대 팀은 12개 동 대표 선수들, 우승 상금 30만 원"
    only_retelling = json.loads(json.dumps(DRAFT))
    for s in only_retelling["sentences"]:
        s["evidence"] = s["evidence"] or ev
    flags = _review_json(tmp_path / "a", only_retelling)["script_flags"]
    assert any(f.startswith("우리 해석 문장이 없음") for f in flags)

    with_take = json.loads(json.dumps(only_retelling))
    with_take["sentences"][3].update(evidence=None, take=True)    # 마지막 본문 문장이 우리 해석
    flags = _review_json(tmp_path / "b", with_take)["script_flags"]
    assert not any("해석" in f for f in flags)
