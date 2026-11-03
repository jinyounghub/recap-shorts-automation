"""images/sensitive.py — 자극적인 그림 판정(가짜 비전 모델)과 흑백/흐림 처리. 실제 API 는 부르지 않는다."""
import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from recap_shorts import pipeline as P
from recap_shorts.images import sensitive
from tests.pipeline.fakes import FakeDeps

STRONG = (200, 20, 20)       # 빨강 → 가짜 모델이 strong
MILD = (200, 170, 20)        # 노랑 → mild
PLAIN = (40, 90, 200)        # 파랑 → none


def stripes(path: Path, color, size=(240, 180)) -> bytes:
    """세로 줄무늬(고주파) 그림 — 흐림을 입히면 표준편차가 크게 줄어든다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    a = np.zeros((size[1], size[0], 3), dtype=np.uint8)
    a[:, :] = color
    a[:, ::4] = [int(c * 0.3) for c in color]
    Image.fromarray(a).save(path)
    return path.read_bytes()


def saturation(path: Path) -> int:
    a = np.asarray(Image.open(path).convert("RGB"), dtype=int)
    return int((a.max(axis=2) - a.min(axis=2)).max())


def spread(path: Path) -> float:
    return float(np.asarray(Image.open(path).convert("L"), dtype=float).std())


class FakeVision:
    """그림의 평균 색으로 판정하는 가짜 비전 모델. 호출마다 받은 그림 수를 기록."""

    def __init__(self, fail=None, blocked=False, drop=None):
        self.calls: list[int] = []
        self.prompts: list[str] = []
        self.fail, self.blocked, self.drop = fail, blocked, drop

    def __call__(self, images, instruction):
        self.calls.append(len(images))
        self.prompts.append(instruction)
        if self.fail:
            raise RuntimeError(self.fail)
        if self.blocked:
            raise RuntimeError("Codex 응답에 후보가 없음: {'blockReason': 'PROHIBITED_CONTENT'}")
        items = []
        for i, im in enumerate(images, 1):
            r, g, b = (int(v) for v in im.resize((1, 1), Image.BOX).getpixel((0, 0)))
            if r > 150 and g < 80:
                items.append({"i": i, "level": "strong", "kind": "gore_body", "reason": "심한 부상 부위가 보임"})
            elif r > 150 and g >= 120:
                items.append({"i": i, "level": "mild", "kind": "blood_injury", "reason": "피가 보임"})
            else:
                items.append({"i": i, "level": "none", "kind": "none", "reason": ""})
        if self.drop:
            items = [x for x in items if x["i"] != self.drop]
        return {"items": items}


@pytest.fixture
def cfg():
    return {**sensitive.DEFAULTS}


def job_with(tmp_path, specs, kind="cap"):
    """specs = [(이름, 색)] → (작업 폴더, assets 항목들, 처음 파일 바이트들)"""
    entries, raw = [], {}
    for name, color in specs:
        rel = f"assets/{kind}/{name}.png"
        raw[rel] = stripes(tmp_path / rel, color)
        entries.append({"file": rel, "kind": kind, "status": "ok", "needs_review": False, "review_reason": None})
    return tmp_path, entries, raw


def test_none_is_untouched(tmp_path, cfg):
    d, ents, raw = job_with(tmp_path, [("a", PLAIN)])
    vis = FakeVision()
    res = sensitive.run(d, ents, read_fn=vis, cfg=cfg)
    assert (d / ents[0]["file"]).read_bytes() == raw[ents[0]["file"]]
    assert not sensitive.orig_path(d / ents[0]["file"]).exists()
    assert ents[0]["sensitive"]["level"] == "none" and ents[0]["sensitive"]["treatment"] == "none"
    assert ents[0]["review_reason"] is None and not ents[0]["needs_review"]
    assert res["checked"] == 1 and res["treated"] == {"gray": 0, "gray_blur": 0} and vis.calls == [1]


def test_mild_is_grayscale(tmp_path, cfg):
    d, ents, raw = job_with(tmp_path, [("a", MILD)])
    sensitive.run(d, ents, read_fn=FakeVision(), cfg=cfg)
    p = d / ents[0]["file"]
    assert saturation(p) == 0 and saturation(sensitive.orig_path(p)) > 50          # 흑백이 됐고 원본은 색이 남아 있음
    assert sensitive.orig_path(p).read_bytes() == raw[ents[0]["file"]]
    s = ents[0]["sensitive"]
    assert (s["level"], s["kind"], s["treatment"]) == ("mild", "blood_injury", "gray") and s["reason"] == "피가 보임"
    assert "흑백 처리" in ents[0]["review_reason"] and "피가 보임" in ents[0]["review_reason"]
    assert not ents[0]["needs_review"]                                               # mild 는 사람 확인 표시 없음
    assert spread(p) > 5                                                             # 흑백만 — 흐리지 않음


def test_strong_is_gray_blur_and_needs_review(tmp_path, cfg):
    d, ents, raw = job_with(tmp_path, [("a", STRONG)])
    with Image.open(d / ents[0]["file"]) as im:
        before = float(np.asarray(im.convert("L"), dtype=float).std())
    sensitive.run(d, ents, read_fn=FakeVision(), cfg=cfg)
    p = d / ents[0]["file"]
    assert saturation(p) == 0
    assert spread(p) < 0.3 * before                                                  # 줄무늬가 뭉개짐(흐림)
    s = ents[0]["sensitive"]
    assert s["treatment"] == "gray_blur" and s["level"] == "strong" and s["flagged"] is True
    assert ents[0]["needs_review"] is True and "흑백+흐림" in ents[0]["review_reason"]
    assert sensitive.orig_path(p).read_bytes() == raw[ents[0]["file"]]


def test_strong_keeps_existing_needs_review_unflagged(tmp_path, cfg):
    d, ents, _ = job_with(tmp_path, [("a", STRONG)])
    ents[0].update(needs_review=True, review_reason="얼굴·아이디가 보일 수 있음 — 가림 확인")
    sensitive.run(d, ents, read_fn=FakeVision(), cfg=cfg)
    assert "flagged" not in ents[0]["sensitive"]                                     # 이미 켜져 있던 표시는 이 단계에서 켠 것이 아님
    assert ents[0]["review_reason"].startswith("얼굴·아이디가 보일 수 있음") and " / " in ents[0]["review_reason"]
    sensitive.set_treatment(d, ents[0], "none", cfg)
    assert ents[0]["needs_review"] is True                                           # 얼굴 확인 표시는 그대로


def test_config_maps_levels(tmp_path):
    cfg = {**sensitive.DEFAULTS, "mild": "none", "strong": "gray"}
    d, ents, _ = job_with(tmp_path, [("m", MILD), ("s", STRONG)])
    sensitive.run(d, ents, read_fn=FakeVision(), cfg=cfg)
    assert ents[0]["sensitive"]["treatment"] == "none" and not sensitive.orig_path(d / ents[0]["file"]).exists()
    assert ents[1]["sensitive"]["treatment"] == "gray" and saturation(d / ents[1]["file"]) == 0
    assert spread(d / ents[1]["file"]) > 5                                           # 흑백만 — 흐리지 않음
    assert "처리 안 함" in ents[0]["review_reason"]                                    # 판정은 알려 줌


def test_load_config_defaults_and_bad_values(tmp_path):
    assert sensitive.load_config(tmp_path / "none.json") == sensitive.DEFAULTS
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"mild": "blur?", "strong": "none", "blur_sigma": "x", "enabled": False, "_doc": "z"}), encoding="utf-8")
    c = sensitive.load_config(p)
    assert c["mild"] == "gray" and c["strong"] == "none" and c["enabled"] is False and c["blur_sigma"] == 28.0
    committed = sensitive.load_config()                                               # 저장소의 config/sensitive.json
    assert committed["enabled"] and committed["mild"] == "gray" and committed["strong"] == "gray_blur"


def test_undo_restores_bytes_and_user_choice_is_remembered(tmp_path, cfg):
    d, ents, raw = job_with(tmp_path, [("a", STRONG)])
    sensitive.run(d, ents, read_fn=FakeVision(), cfg=cfg)
    e, p = ents[0], d / ents[0]["file"]
    sensitive.set_treatment(d, e, "none", cfg)
    assert p.read_bytes() == raw[e["file"]]                                          # 되돌림
    assert e["sensitive"]["treatment"] == "none_by_user" and e["sensitive"]["user"] is True
    assert e["needs_review"] is False and "flagged" not in e["sensitive"]            # 사람이 정했으니 확인 표시 꺼짐
    assert "원래대로" in e["review_reason"] and "흑백+흐림" not in e["review_reason"]
    # 다른 처리 고르기 — 항상 원본에서 다시 만든다
    sensitive.set_treatment(d, e, "gray", cfg)
    assert saturation(p) == 0 and spread(p) > 5 and e["sensitive"]["treatment"] == "gray"
    sensitive.set_treatment(d, e, "gray_blur", cfg)
    assert spread(p) < 5 and sensitive.orig_path(p).read_bytes() == raw[e["file"]]
    sensitive.set_treatment(d, e, "none", cfg)
    # 같은 그림이 새 항목으로 다시 들어와도(재실행) 사람의 선택을 지키고 다시 묻지 않는다
    fresh = [{"file": e["file"], "kind": "cap", "status": "ok"}]
    vis = FakeVision()
    sensitive.run(d, fresh, read_fn=vis, cfg=cfg)
    assert vis.calls == [] and fresh[0]["sensitive"]["treatment"] == "none_by_user"
    assert p.read_bytes() == raw[e["file"]] and not fresh[0].get("needs_review")


def test_set_treatment_rejects_bad_input_and_missing_orig(tmp_path, cfg):
    d, ents, _ = job_with(tmp_path, [("a", STRONG)])
    sensitive.run(d, ents, read_fn=FakeVision(), cfg=cfg)
    with pytest.raises(ValueError):
        sensitive.set_treatment(d, ents[0], "blur", cfg)
    sensitive.orig_path(d / ents[0]["file"]).unlink()
    with pytest.raises(FileNotFoundError):
        sensitive.set_treatment(d, ents[0], "none", cfg)


def test_user_can_treat_an_unflagged_picture(tmp_path, cfg):
    d, ents, raw = job_with(tmp_path, [("a", PLAIN)])
    sensitive.run(d, ents, read_fn=FakeVision(), cfg=cfg)
    sensitive.set_treatment(d, ents[0], "gray_blur", cfg)
    p = d / ents[0]["file"]
    assert saturation(p) == 0 and sensitive.orig_path(p).read_bytes() == raw[ents[0]["file"]]
    sensitive.set_treatment(d, ents[0], "none", cfg)
    assert p.read_bytes() == raw[ents[0]["file"]]


def test_option_off_makes_no_call(tmp_path):
    d, ents, raw = job_with(tmp_path, [("a", STRONG)])
    vis = FakeVision()
    res = sensitive.run(d, ents, read_fn=vis, cfg={**sensitive.DEFAULTS, "enabled": False})
    assert res.get("disabled") and vis.calls == [] and "sensitive" not in ents[0]
    assert (d / ents[0]["file"]).read_bytes() == raw[ents[0]["file"]]


def test_check_ai_false_skips_ai_pictures(tmp_path):
    d, ents, _ = job_with(tmp_path, [("a", STRONG)], kind="ai")
    vis = FakeVision()
    sensitive.run(d, ents, read_fn=vis, cfg={**sensitive.DEFAULTS, "check_ai": False})
    assert vis.calls == [] and "sensitive" not in ents[0]


def test_failure_leaves_pictures_untouched(tmp_path, cfg):
    d, ents, raw = job_with(tmp_path, [("a", STRONG), ("b", MILD)])
    res = sensitive.run(d, ents, read_fn=FakeVision(fail="api down"), cfg=cfg)
    assert len(res["errors"]) == 2 and "api down" in res["errors"][0] and res["checked"] == 0
    assert all("sensitive" not in e for e in ents)
    assert all((d / e["file"]).read_bytes() == raw[e["file"]] for e in ents)
    assert not (d / sensitive.CACHE_REL).exists()                                     # 실패는 기록하지 않음 → 다음에 다시 시도


def test_missing_answer_for_one_picture_is_an_error_for_that_one(tmp_path, cfg):
    d, ents, raw = job_with(tmp_path, [("a", STRONG), ("b", MILD), ("c", PLAIN)])
    res = sensitive.run(d, ents, read_fn=FakeVision(drop=2), cfg=cfg)
    assert res["checked"] == 2 and len(res["errors"]) == 1
    assert "sensitive" not in ents[1] and (d / ents[1]["file"]).read_bytes() == raw[ents[1]["file"]]
    assert ents[0]["sensitive"]["treatment"] == "gray_blur"


def test_cache_hit_makes_no_second_call(tmp_path, cfg):
    d, ents, raw = job_with(tmp_path, [("a", STRONG), ("b", MILD), ("c", PLAIN)])
    vis = FakeVision()
    sensitive.run(d, ents, read_fn=vis, cfg=cfg)
    assert vis.calls == [3]
    doc = json.loads((d / sensitive.CACHE_REL).read_text(encoding="utf-8"))
    assert len(doc["items"]) == 3 and all(k in doc for k in ("prompt", "model", "user", "applied"))
    # 카드를 다시 만든 것처럼 처리 전 그림으로 새 항목을 만든다 → 기록에서 읽음
    for e in ents:
        p = d / e["file"]
        o = sensitive.orig_path(p)
        if o.exists():
            p.write_bytes(o.read_bytes())
            o.unlink()
    fresh = [{"file": e["file"], "kind": "cap", "status": "ok"} for e in ents]
    vis2 = FakeVision()
    res = sensitive.run(d, fresh, read_fn=vis2, cfg=cfg)
    assert vis2.calls == [] and res["calls"] == 0 and res["cached"] == 3
    assert [f["sensitive"]["treatment"] for f in fresh] == ["gray_blur", "gray", "none"]
    assert sensitive.orig_path(d / fresh[0]["file"]).read_bytes() == raw[fresh[0]["file"]]


def test_already_treated_file_is_adopted_not_reclassified(tmp_path, cfg):
    """대상 사진 캐시처럼 카드를 다시 만들지 않는 재실행 — 처리된 파일을 원본으로 착각해 .orig 를 덮어쓰면 안 된다."""
    d, ents, raw = job_with(tmp_path, [("a", STRONG)])
    sensitive.run(d, ents, read_fn=FakeVision(), cfg=cfg)
    p = d / ents[0]["file"]
    treated = p.read_bytes()
    fresh = [{"file": ents[0]["file"], "kind": "cap", "status": "ok", "needs_review": False}]
    vis = FakeVision()
    sensitive.run(d, fresh, read_fn=vis, cfg=cfg)
    assert vis.calls == [] and p.read_bytes() == treated
    assert sensitive.orig_path(p).read_bytes() == raw[ents[0]["file"]]               # 원본 그대로
    assert fresh[0]["sensitive"]["treatment"] == "gray_blur" and fresh[0]["needs_review"] is True
    sensitive.set_treatment(d, fresh[0], "none", cfg)
    assert p.read_bytes() == raw[ents[0]["file"]]


def test_already_recorded_entries_are_skipped(tmp_path, cfg):
    d, ents, _ = job_with(tmp_path, [("a", STRONG)], kind="ai")
    sensitive.run(d, ents, read_fn=FakeVision(), cfg=cfg)
    treated = (d / ents[0]["file"]).read_bytes()
    vis = FakeVision()
    sensitive.run(d, ents, read_fn=vis, cfg=cfg)                                       # 이미 기록이 있는 항목(AI 이미지를 다시 쓰는 재실행)
    assert vis.calls == [] and (d / ents[0]["file"]).read_bytes() == treated


def test_batches_of_eight_and_alignment(tmp_path, cfg):
    specs = [(f"p{i:02d}", STRONG if i % 5 == 0 else MILD if i % 5 == 1 else PLAIN) for i in range(19)]
    d, ents, _ = job_with(tmp_path, specs)
    # 같은 색이라도 파일 내용이 다르게
    for i, e in enumerate(ents):
        a = np.asarray(Image.open(d / e["file"]).convert("RGB")).copy()
        a[0, 0] = (i, 0, 0)
        Image.fromarray(a).save(d / e["file"])
    vis = FakeVision()
    res = sensitive.run(d, ents, read_fn=vis, cfg=cfg)
    assert vis.calls == [8, 8, 3] and res["calls"] == 3 and res["checked"] == 19
    for i, e in enumerate(ents):
        want = "strong" if i % 5 == 0 else "mild" if i % 5 == 1 else "none"
        assert e["sensitive"]["level"] == want, (i, e["sensitive"])
    assert all("{n}" not in p and "19" not in p for p in vis.prompts[:1])


def test_identical_pictures_are_sent_once(tmp_path, cfg):
    d, ents, _ = job_with(tmp_path, [("a", STRONG)])
    (d / "assets/cap/b.png").write_bytes((d / "assets/cap/a.png").read_bytes())
    ents.append({"file": "assets/cap/b.png", "kind": "cap", "status": "ok"})
    vis = FakeVision()
    sensitive.run(d, ents, read_fn=vis, cfg=cfg)
    assert vis.calls == [1] and all(e["sensitive"]["treatment"] == "gray_blur" for e in ents)


def test_blocked_by_model_counts_as_strong(tmp_path, cfg):
    d, ents, _ = job_with(tmp_path, [("a", PLAIN)])
    res = sensitive.run(d, ents, read_fn=FakeVision(blocked=True), cfg=cfg)
    s = ents[0]["sensitive"]
    assert s["level"] == "strong" and s["blocked"] is True and s["treatment"] == "gray_blur" and not res["errors"]


def test_blocked_batch_is_split_into_single_calls(tmp_path, cfg):
    d, ents, _ = job_with(tmp_path, [("a", PLAIN), ("b", MILD)])
    calls = []

    def read(images, instr):
        calls.append(len(images))
        if len(images) > 1:
            raise RuntimeError("Codex 응답이 비어 있음(finishReason=SAFETY)")
        r = int(images[0].resize((1, 1), Image.BOX).getpixel((0, 0))[0])
        if r > 150:
            return {"items": [{"i": 1, "level": "mild", "kind": "blood_injury", "reason": "피"}]}
        return {"items": [{"i": 1, "level": "none", "kind": "none", "reason": ""}]}
    sensitive.run(d, ents, read_fn=read, cfg=cfg)
    assert calls == [2, 1, 1] and [e["sensitive"]["level"] for e in ents] == ["none", "mild"]


def test_two_failed_calls_in_a_row_stop_asking(tmp_path, cfg):
    d, ents, _ = job_with(tmp_path, [(f"p{i}", PLAIN) for i in range(30)])
    for i, e in enumerate(ents):
        a = np.asarray(Image.open(d / e["file"]).convert("RGB")).copy()
        a[0, 0] = (i, 1, 1)
        Image.fromarray(a).save(d / e["file"])
    vis = FakeVision(fail="HTTP 503")
    res = sensitive.run(d, ents, read_fn=vis, cfg=cfg)
    assert vis.calls == [8, 8] and len(res["errors"]) == 30


def test_parse_tolerates_odd_answers():
    assert sensitive._parse({"items": [{"level": "MILD", "kind": "blood_injury"}, {"level": "none"}]}, 2)[1]["level"] == "mild"
    assert sensitive._parse({"items": [{"level": "mild"}]}, 2) == {}                  # 번호 없고 개수가 다르면 믿지 않음
    rows = sensitive._parse({"items": [{"i": 1, "level": "strong", "kind": "weird", "reason": " 길게  쓴 이유 "},
                                       {"i": 2, "level": "huge"}, {"i": 9, "level": "mild"}, {"i": 1, "level": "none"}]}, 2)
    assert rows == {1: {"level": "strong", "kind": "other", "reason": "길게 쓴 이유"}}
    assert sensitive._parse("nope", 1) == {} and sensitive._parse({"items": "x"}, 1) == {}


def test_prompt_mentions_levels_and_text_only_rule():
    for w in ("none", "mild", "strong", "blood_injury", "gore_body", "corpse_death", "accident_disaster", "violence_weapon",
              "animal_harm", "medical_procedure", "sexual_suggestive", "글만 있는", "광고주 친화"):
        assert w in sensitive.INSTRUCTION
    assert sensitive.INSTRUCTION.count("{n}") >= 1


def test_cache_dropped_when_prompt_changes_but_user_choices_stay(tmp_path, cfg, monkeypatch):
    d, ents, _ = job_with(tmp_path, [("a", STRONG)])
    sensitive.run(d, ents, read_fn=FakeVision(), cfg=cfg)
    sensitive.set_treatment(d, ents[0], "gray", cfg)
    monkeypatch.setattr(sensitive, "PROMPT_V", "other")
    c = sensitive.Cache(d / sensitive.CACHE_REL)
    assert c.items == {} and len(c.user) == 1 and len(c.applied) >= 1


def test_after_remake_reapplies_treatment(tmp_path, cfg):
    d, ents, raw = job_with(tmp_path, [("a", STRONG)])
    sensitive.run(d, ents, read_fn=FakeVision(), cfg=cfg)
    e, p = ents[0], d / ents[0]["file"]
    new = stripes(p, (210, 30, 30), size=(240, 180))                                   # 가리기가 카드를 새 그림으로 덮어씀(처리 풀림)
    assert saturation(p) > 50
    e.update(needs_review=False, review_reason="가림 확인됨")
    sensitive.after_remake(d, e, cfg)
    assert saturation(p) == 0 and spread(p) < 5                                        # 다시 흑백+흐림
    assert sensitive.orig_path(p).read_bytes() == new                                  # 원본은 새 카드
    assert "흑백+흐림" in e["review_reason"] and e["review_reason"].startswith("가림 확인됨")
    sensitive.set_treatment(d, e, "none", cfg)
    assert p.read_bytes() == new


def test_forget_removes_orig_and_note(tmp_path, cfg):
    d, ents, _ = job_with(tmp_path, [("a", STRONG)], kind="ai")
    sensitive.run(d, ents, read_fn=FakeVision(), cfg=cfg)
    sensitive.forget(d, ents[0])
    assert "sensitive" not in ents[0] and not sensitive.orig_path(d / ents[0]["file"]).exists()
    assert ents[0]["needs_review"] is False and not ents[0]["review_reason"]


# ------------------------------------------------------------------ 파이프라인·API 연결
class SensDeps(FakeDeps):
    """AI 이미지(초록)는 mild, 나머지는 none 으로 보는 가짜 비전."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.vision_calls: list[int] = []
        self.fail_vision = False

    def sensitive_read(self, images, instruction):
        self.vision_calls.append(len(images))
        if self.fail_vision:
            raise RuntimeError("Codex 연결 실패")
        out = []
        for i, im in enumerate(images, 1):
            r, g, b = (int(v) for v in im.resize((1, 1), Image.BOX).getpixel((0, 0)))
            mild = g > 120 and r < 60
            out.append({"i": i, "level": "mild" if mild else "none", "kind": "blood_injury" if mild else "none",
                        "reason": "피가 보임" if mild else ""})
        return {"items": out}


def make_job(tmp_path, deps, options):
    store = P.JobStore(tmp_path)
    runner = P.Runner(store, deps)
    runner.start()
    job = store.create({"gallery": "baseball_new13", "no": 2101,
                        "url": "https://gall.dcinside.com/board/view/?id=baseball_new13&no=2101", "minor": False}, options)
    runner.submit(job)
    assert runner.wait_idle(60)
    return job


def sub_of(job, label):
    return next((x for x in job.step("assets")["sub"] if x["label"] == label), None)


def test_pipeline_treats_ai_pictures_and_shows_sub_row(tmp_path):
    deps = SensDeps()
    job = make_job(tmp_path, deps, {"auto_steps": ["read", "script"]})
    ents = json.loads((job.dir / "assets/assets.json").read_text(encoding="utf-8"))
    ai = [e for e in ents if e["kind"] == "ai" and e["status"] == "ok"]
    assert ai and all(e["sensitive"]["treatment"] == "gray" for e in ai)
    assert all(saturation(job.dir / e["file"]) == 0 and sensitive.orig_path(job.dir / e["file"]).is_file() for e in ai)
    caps = [e for e in ents if e["kind"] == "cap" and e.get("role") != "dc_head"]
    assert caps and all(e["sensitive"]["level"] == "none" for e in caps)
    assert not any(e.get("role") == "dc_head" and "sensitive" in e for e in ents)       # 글 머리 카드는 검사하지 않음
    row = sub_of(job, "민감한 그림")
    assert row["status"] == "done" and f"흑백 {len(ai)}장" in row["note"] and "Codex" in row["note"]
    assert max(deps.vision_calls) <= 8
    assert (job.dir / sensitive.CACHE_REL).is_file()
    # 렌더용 목록(image 산출물)에는 처리된 파일이 같은 이름으로 들어 있다
    assert all(e["file"] in job.state["artifacts"]["images"] for e in ai)


def test_pipeline_option_off_makes_no_calls(tmp_path):
    deps = SensDeps()
    job = make_job(tmp_path, deps, {"auto_steps": ["read", "script"], "sensitive_treatment": False})
    ents = json.loads((job.dir / "assets/assets.json").read_text(encoding="utf-8"))
    assert deps.vision_calls == [] and not any("sensitive" in e for e in ents)
    assert sub_of(job, "민감한 그림") is None and not (job.dir / sensitive.CACHE_REL).exists()
    ai = [e for e in ents if e["kind"] == "ai"]
    assert ai and all(saturation(job.dir / e["file"]) > 50 for e in ai)


def test_pipeline_config_disabled_makes_no_calls(tmp_path, monkeypatch):
    p = tmp_path / "off.json"
    p.write_text(json.dumps({"enabled": False}), encoding="utf-8")
    monkeypatch.setattr(sensitive, "CONFIG_PATH", p)
    deps = SensDeps()
    make_job(tmp_path / "w", deps, {"auto_steps": ["read", "script"]})
    assert deps.vision_calls == []


def test_pipeline_failure_continues_with_error_note(tmp_path):
    deps = SensDeps()
    deps.fail_vision = True
    job = make_job(tmp_path, deps, {"auto_steps": ["read", "script"]})
    assert job.step("scenes")["status"] == "waiting"                                  # 다른 단계는 계속
    ents = json.loads((job.dir / "assets/assets.json").read_text(encoding="utf-8"))
    assert not any("sensitive" in e for e in ents)
    ai = [e for e in ents if e["kind"] == "ai"]
    assert ai and all(saturation(job.dir / e["file"]) > 50 for e in ai)               # 처리 없음
    row = sub_of(job, "민감한 그림")
    assert row["status"] == "error" and "검사 못 함" in row["note"] and "연결 실패" in row["note"]


def test_pipeline_rerun_does_not_ask_again(tmp_path):
    """자료 화면 단계를 다시 돌려도(카드는 다시 만들어짐) 같은 그림은 기록에서 읽는다."""
    deps = SensDeps()
    job = make_job(tmp_path, deps, {"auto_steps": ["read", "script"]})
    first = len(deps.vision_calls)
    assert first >= 1
    store_runner = P.Runner(P.JobStore(tmp_path), deps)
    job2 = store_runner.store.get(job.id)
    job2.reset_from("assets")
    store_runner.start()
    store_runner.submit(job2)
    assert store_runner.wait_idle(60)
    ents = json.loads((job2.dir / "assets/assets.json").read_text(encoding="utf-8"))
    ai = [e for e in ents if e["kind"] == "ai" and e["status"] == "ok"]
    assert ai and all(e["sensitive"]["treatment"] == "gray" and saturation(job2.dir / e["file"]) == 0 for e in ai)
    assert len(deps.vision_calls) == first                                            # 두 번째 실행은 호출 0회
    assert all(sensitive.orig_path(job2.dir / e["file"]).is_file() for e in ai)


# ------------------------------------------------------------------ 평가용 명령
def test_cli_prints_and_writes_json(tmp_path, monkeypatch, capsys):
    stripes(tmp_path / "set" / "a.png", STRONG)
    stripes(tmp_path / "set" / "sub" / "b.png", PLAIN)
    (tmp_path / "set" / "notes.txt").write_text("x", encoding="utf-8")
    from recap_shorts.llm import codex
    vis = FakeVision()
    monkeypatch.setattr(codex, "read_images", lambda imgs, instr, model=None, json=False, thinking=None, **k: (
        vis(imgs, instr) if thinking == "low" and json else (_ for _ in ()).throw(AssertionError("thinking low, json 이어야 함"))))
    out = tmp_path / "out.json"
    rc = sensitive.main([str(tmp_path / "set"), "--json", str(out), "--no-cache"])
    assert rc == 0 and vis.calls == [2]
    text = capsys.readouterr().out
    assert "strong" in text and "gore_body" in text and "2장" in text
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["calls"] == 1 and len(doc["items"]) == 2 and {x["level"] for x in doc["items"]} == {"strong", "none"}
    # 기록 켜고 두 번 → 두 번째는 호출 없음
    cache = tmp_path / "cli_cache.json"
    sensitive.main([str(tmp_path / "set"), "--cache", str(cache)])
    sensitive.main([str(tmp_path / "set"), "--cache", str(cache)])
    assert vis.calls == [2, 2]


def test_cli_error_exit_code(tmp_path, monkeypatch, capsys):
    stripes(tmp_path / "a.png", STRONG)
    from recap_shorts.llm import codex

    def boom(*a, **k):
        raise RuntimeError("no key")
    monkeypatch.setattr(codex, "read_images", boom)
    assert sensitive.main([str(tmp_path / "a.png"), "--no-cache"]) == 1
    assert "오류" in capsys.readouterr().out
    assert sensitive.main([str(tmp_path / "nothing_here")]) == 2
