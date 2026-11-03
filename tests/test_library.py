"""효과음·짤 폴더 분석(fx/library.py, analyze_sfx.py, analyze_meme.py) — Codex 대신 가짜 답(llm=)으로 끝까지 돌린다.
꾸러미 자리는 시험마다 임시 폴더(RECAP_LIBRARY_DIR), 설정 파일도 임시(tests/conftest.py)."""
import json
import re
import shutil
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from recap_shorts import settings
from recap_shorts.fx import analyze_meme as AM
from recap_shorts.fx import analyze_sfx as AS
from recap_shorts.fx import apply as A
from recap_shorts.fx import library as L

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg 없음")


@pytest.fixture(autouse=True)
def _lib_root(tmp_path, monkeypatch):
    monkeypatch.setenv("RECAP_LIBRARY_DIR", str(tmp_path / "analyzed"))


def tone(path: Path, dur=0.4, freq=800, lead=0.2, sr=48000, amp=0.6):
    """앞에 lead 초 무음이 있는 짧은 소리."""
    path.parent.mkdir(parents=True, exist_ok=True)
    t = np.arange(int(sr * dur)) / sr
    x = np.concatenate([np.zeros(int(sr * lead)), amp * np.sin(2 * np.pi * freq * t) * np.exp(-t * 6)])
    sf.write(path, x.astype(np.float32), sr, subtype="PCM_16")


def stems(parts):
    return [re.search(r'"(.*)"', p["text"]).group(1) for p in parts[1:] if "text" in p]


class FakeEar:
    """파일 이름(stem) → 들은 결과. 불린 횟수를 센다. as_text 면 모델처럼 글(울타리 친 JSON 배열)로 답한다."""
    def __init__(self, table, as_text=False):
        self.table, self.calls, self.seen, self.as_text = table, 0, [], as_text

    def __call__(self, parts):
        self.calls += 1
        out = []
        for k, s in enumerate(stems(parts), 1):
            self.seen.append(s)
            if s in self.table:
                out.append({"k": f"#{k}" if k % 2 else k, **self.table[s]})
        return f"답입니다.\n```json\n{json.dumps(out, ensure_ascii=False)}\n```" if self.as_text else out


def sfx_folder(tmp_path):
    d = tmp_path / "내 효과음"
    tone(d / "pop_01.wav", freq=1200)            # 이름으로 종류를 정한 파일
    tone(d / "a.wav", freq=900)
    tone(d / "b.wav", freq=500)
    tone(d / "sub" / "c.wav", dur=1.2, freq=120)
    shutil.copyfile(d / "sub" / "c.wav", d / "sub" / "c (2).wav")   # 같은 소리 사본
    tone(d / "song.wav", dur=2.0, freq=440)
    (d / "readme.txt").write_text("x", encoding="utf-8")
    return d


EAR = {
    "a": {"desc": "짧은 뽁", "speech": "none", "music": False, "source": "generic", "quality": 4, "fit": {"pop": 3}},
    "b": {"desc": "욕", "speech": "word", "words": "xx", "offensive": True, "quality": 4, "fit": {"huh": 2}},
    "c": {"desc": "쿵", "speech": "none", "quality": 5, "fit": {"boom": 3, "pop": 1}},
    "c (2)": {"desc": "쿵", "speech": "none", "quality": 5, "fit": {"boom": 3}},
    "song": {"desc": "노래", "music": True, "source": "song", "quality": 4, "fit": {"punchline": 2}},
    "pop_01": {"desc": "톡", "quality": 3, "fit": {"pop": 2}},
}


def test_parse_answer_list():
    rows = [{"k": 1, "desc": "a"}, {"k": 2}]
    assert AS.parse_answer_list(json.dumps(rows)) == rows
    assert AS.parse_answer_list("앞말 ```json\n" + json.dumps(rows) + "\n``` 뒷말") == rows
    assert AS.parse_answer_list(json.dumps({"items": rows})) == rows
    assert AS.parse_answer_list('{"k": 1}') == [{"k": 1}]
    assert AS.parse_answer_list("모르겠음") == []
    batch = [("a.wav", None), ("b.wav", None)]
    assert [AS.batch_key(r, batch) for r in ({"k": "#2"}, {"k": 1}, {"k": 3}, {"k": "x"}, {})] == ["b.wav", "a.wav", None, None, None]


def test_sfx_analysis_picks_trims_and_builds_pack(tmp_path):
    src = sfx_folder(tmp_path)
    before = sorted(p.relative_to(src).as_posix() for p in src.rglob("*"))
    ear = FakeEar(EAR, as_text=True)
    s = L.start("sfx", str(src), use=True, llm=ear, wait=True)
    assert s["state"] == "done" and s["listened"] and s["used"]
    doc = L.get("sfx", s["id"])
    by = {it["key"]: it for it in doc["items"]}
    assert by["a.wav"]["auto"] == {"keep": True, "type": "pop", "why": []}
    assert by["pop_01.wav"]["auto"]["keep"] and by["pop_01.wav"]["auto"]["type"] == "pop"
    assert by["b.wav"]["auto"]["why"] == ["욕설·성적"]
    assert by["song.wav"]["auto"]["why"][:1] == ["음악"]
    assert by["sub/c.wav"]["auto"]["type"] == "boom"
    assert by["sub/c (2).wav"]["auto"]["why"][0].startswith("같은 소리")
    pack = Path(s["out_dir"])
    man = json.loads((pack / "manifest.json").read_text(encoding="utf-8"))["items"]
    got = {(r["type"], Path(r["source_file"]).name) for r in man}
    assert got == {("pop", "pop_01.wav"), ("pop", "a.wav"), ("boom", "c.wav")}
    first_pop = next(r for r in man if r["id"] == "pop_01")
    assert Path(first_pop["source_file"]).name == "pop_01.wav" and "hook_first_cut" in first_pop["roles"]
    # 다듬기: 앞 무음 0.2초를 떼고 pop 은 0.6초 상한, 피크 −1 dBFS
    y, sr = sf.read(pack / "pop_02.wav")
    assert len(y) / sr <= 0.6 + 1e-3 and abs(np.abs(y).max() - 10 ** (-1 / 20)) < 0.01
    assert abs(y[: int(0.01 * sr)]).max() > 0.01          # 무음이 아니라 바로 소리로 시작
    assert (pack / "_들어보기_전체.wav").is_file()
    # 원본 폴더는 그대로(파일이 하나도 늘지 않음)
    assert sorted(p.relative_to(src).as_posix() for p in src.rglob("*")) == before
    # 설정: 꾸러미 폴더가 들어갔고 화면에는 원본 경로로 보인다
    assert settings.sfx_dirs() == [str(pack.resolve())]
    view = settings.sfx_view()["dirs"][0]
    assert view["label"] == str(src.resolve()) and view["pack"] == {"kind": "sfx", "id": s["id"]}
    lib, notes, used = A.choose_sfx_library({"libraries": []})
    assert used and {it["type"] for it in lib} == {"pop", "boom"}


def test_sfx_reanalysis_reuses_results_and_keeps_human_changes(tmp_path):
    src = sfx_folder(tmp_path)
    ear = FakeEar(EAR)
    s = L.start("sfx", str(src), llm=ear, wait=True)
    calls = ear.calls
    d = L.apply_changes("sfx", s["id"], [{"key": "b.wav", "keep": True, "type": "huh"},
                                          {"key": "a.wav", "keep": False}])
    by = {it["key"]: it for it in d["items"]}
    assert by["b.wav"]["keep"] and by["b.wav"]["type"] == "huh" and by["b.wav"]["out"]["id"] == "huh_01"
    assert not by["a.wav"]["keep"] and by["a.wav"]["out"] is None
    assert not (Path(s["out_dir"]) / "pop_02.wav").exists()       # 뺀 소리의 파일도 지움
    # 새 파일 하나만 더하고 다시 분석: 그 파일만 듣고, 사람이 고친 것은 남는다
    tone(src / "new.wav", freq=2000)
    ear.table = {**EAR, "new": {"desc": "딩", "quality": 4, "fit": {"ding": 3}}}
    ear.seen.clear()
    s2 = L.start("sfx", str(src), llm=ear, wait=True)
    assert ear.calls == calls + 1 and ear.seen == ["new"]
    by = {it["key"]: it for it in L.detail(L.get("sfx", s2["id"]))["items"]}
    assert by["b.wav"]["type"] == "huh" and not by["a.wav"]["keep"] and by["new.wav"]["type"] == "ding"
    d = L.apply_changes("sfx", s2["id"], [{"key": "a.wav", "reset": True}])
    assert {it["key"]: it for it in d["items"]}["a.wav"]["keep"]


def test_sfx_without_audio_model_uses_local_metadata(tmp_path):
    src = sfx_folder(tmp_path)
    tone(src / "whoosh-sound-effect.wav")             # 규칙이 안 쓰는 종류 이름(내려받은 파일 이름에 흔함)
    s = L.start("sfx", str(src), wait=True)          # 시험 중이라 Codex 를 부르지 않는다 = 키 없음과 같음
    assert s["state"] == "done" and not s["listened"] and "로컬 측정값" in s["message"]
    d = L.detail(L.get("sfx", s["id"]))
    kept = [it["key"] for it in d["items"] if it["keep"]]
    assert kept == ["pop_01.wav"]
    why = {it["key"]: it["auto"]["why"] for it in d["items"]}
    assert why["a.wav"] == ["이름으로 종류를 알 수 없음"] and why["whoosh-sound-effect.wav"] == ["영상 규칙이 안 쓰는 종류(whoosh)"]


def test_named_file_with_unused_type_goes_through_listening(tmp_path):
    src = sfx_folder(tmp_path)
    tone(src / "whoosh-sound-effect.wav")
    ear = FakeEar({**EAR, "whoosh-sound-effect": {"desc": "뽁", "quality": 4, "fit": {"pop": 3}},
                   "pop_01": {"desc": "욕", "offensive": True, "quality": 4, "fit": {"pop": 2}}})
    s = L.start("sfx", str(src), llm=ear, wait=True)
    by = {it["key"]: it for it in L.detail(L.get("sfx", s["id"]))["items"]}
    assert by["whoosh-sound-effect.wav"]["type"] == "pop" and by["whoosh-sound-effect.wav"]["keep"]
    assert by["pop_01.wav"]["auto"]["why"] == ["욕설·성적"]       # 이름으로 정한 파일도 욕설은 뺀다


def test_listen_errors_halt_on_quota_and_resume(tmp_path):
    src = sfx_folder(tmp_path)
    from recap_shorts import errors

    def boom(parts):
        raise errors.mark(RuntimeError("RESOURCE_EXHAUSTED quota"), errors.QUOTA)
    s = L.start("sfx", str(src), llm=boom, wait=True)
    assert s["state"] == "done" and "끝까지 쓰지 못함" in s["message"]
    by = {it["key"]: it for it in L.detail(L.get("sfx", s["id"]))["items"]}
    assert by["a.wav"]["auto"]["why"] == ["듣지 못함"] and by["pop_01.wav"]["keep"]
    ear = FakeEar(EAR)
    L.start("sfx", str(src), llm=ear, wait=True)
    assert "a" in ear.seen


def img(path: Path, size=(200, 200), color=(200, 30, 30), alpha=False, mark=0):
    from PIL import Image, ImageDraw
    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGBA" if alpha else "RGB", size, (0, 0, 0, 0) if alpha else (255, 255, 255))
    d = ImageDraw.Draw(im)
    d.ellipse([20 + mark, 20, size[0] - 20, size[1] - 40 - mark], fill=color)
    d.rectangle([10, size[1] - 30, 10 + mark * 3 + 20, size[1] - 10], fill=(10, 10, 10))
    im.save(path)


EYE = {
    "물음표_개구리": {"reaction": True, "name": "물음표 개구리", "meaning": "고개 갸웃 — '왜?' 하는 의문.", "beats": ["question", "nope"],
                  "family": "pepe", "quality": 4},
    "물음표_개구리_사본": {"reaction": True, "name": "물음표 개구리", "meaning": "같은 그림", "beats": ["question"], "family": "pepe", "quality": 4},
    "화남": {"reaction": True, "name": "화난 얼굴", "meaning": "빨개짐 — '열받네' 하는 분노.", "beats": ["angry"], "family": "cartoon",
           "has_text": True, "text": "아오", "real_person": True, "quality": 3},
    "야한것": {"reaction": True, "name": "x", "meaning": "x", "beats": ["funny"], "sexual": True, "quality": 4},
    "풍경": {"reaction": False, "name": "산", "meaning": "산 사진", "beats": ["neutral"], "quality": 5},
}


def meme_folder(tmp_path):
    from PIL import Image
    d = tmp_path / "짤 모음"
    img(d / "물음표_개구리.png", alpha=True)
    Image.open(d / "물음표_개구리.png").resize((150, 150)).save(d / "물음표_개구리_사본.png")   # 같은 그림(줄인 사본)
    img(d / "화남.jpg", color=(30, 30, 200), mark=40)
    img(d / "야한것.png", color=(30, 200, 30), mark=10)
    img(d / "풍경.webp", color=(90, 90, 90), mark=60)
    img(d / "작은것.png", size=(60, 60))
    return d


def test_meme_analysis_writes_meanings_and_filters(tmp_path):
    src = meme_folder(tmp_path)
    eye = FakeEar(EYE)
    s = L.start("meme", str(src), use=True, llm=eye, wait=True)
    assert s["state"] == "done" and s["used"] and s["counts"]["kept"] == 2
    d = L.detail(L.get("meme", s["id"]))
    by = {it["key"]: it for it in d["items"]}
    assert by["물음표_개구리.png"]["keep"] and by["물음표_개구리.png"]["beats"] == ["question"]   # 모르는 분위기는 버림
    assert by["물음표_개구리_사본.png"]["dup_of"] == "물음표_개구리.png"
    assert by["화남.jpg"]["real_person"] and by["화남.jpg"]["has_text"] and by["화남.jpg"]["keep"]
    assert by["야한것.png"]["auto"]["why"] == ["성적"]
    assert by["풍경.webp"]["auto"]["why"] == ["반응 짤이 아님"]
    assert by["작은것.png"]["auto"]["why"] == ["너무 작음"] and "작은것" not in eye.seen   # 작은 그림은 보여 주지도 않음
    assert by["물음표_개구리.png"]["transparent"]
    man = json.loads((Path(s["out_dir"]) / "manifest.json").read_text(encoding="utf-8"))["items"]
    assert [(r["file"], r["name"]) for r in man] == [("m_001.png", "물음표 개구리"), ("m_002.jpg", "화난 얼굴")]
    assert man[1]["real_person"] and man[1]["source_site"] == ""
    # 영상이 이 꾸러미에서 짤을 고른다
    lib, note = A.choose_meme_library({"library": "assets_local/없는폴더"})
    assert [it["name"] for it in lib] == ["물음표 개구리", "화난 얼굴"] and "설정한 짤 폴더" in note
    mv = settings.memes_view()
    assert mv["using"] == "custom" and mv["dirs"][0]["label"] == str(src.resolve()) and mv["dirs"][0]["count"] == 2


def test_meme_human_edits_and_name_fallback(tmp_path):
    src = meme_folder(tmp_path)
    s = L.start("meme", str(src), wait=True)          # Codex 없음 → 파일 이름이 뜻
    d = L.detail(L.get("meme", s["id"]))
    by = {it["key"]: it for it in d["items"]}
    assert not s["listened"] and by["물음표_개구리.png"]["meaning"] == "물음표 개구리"
    assert by["물음표_개구리_사본.png"]["auto"]["why"][0].startswith("같은 그림")
    d = L.apply_changes("meme", s["id"], [{"key": "물음표_개구리.png", "meaning": "갸웃 — '왜?'", "beats": ["question"]},
                                           {"key": "풍경.webp", "keep": False}])
    by = {it["key"]: it for it in d["items"]}
    assert by["물음표_개구리.png"]["meaning"] == "갸웃 — '왜?'" and by["물음표_개구리.png"]["user"]["beats"] == ["question"]
    man = json.loads((Path(s["out_dir"]) / "manifest.json").read_text(encoding="utf-8"))["items"]
    assert "풍경" not in {Path(r["source_file"]).stem for r in man}
    assert all((Path(s["out_dir"]) / r["file"]).is_file() for r in man)
    assert next(r for r in man if Path(r["source_file"]).stem == "물음표_개구리")["meaning"] == "갸웃 — '왜?'"
    with pytest.raises(L.LibraryError):
        L.apply_changes("meme", s["id"], [{"key": "물음표_개구리.png", "beats": ["모름"]}])
    with pytest.raises(L.LibraryError):
        L.apply_changes("meme", s["id"], [{"key": "없음.png", "keep": True}])


def test_files_add_delete_and_guards(tmp_path):
    src = sfx_folder(tmp_path)
    r = L.add("sfx", str(src))                       # manifest 없음 → 분석 시작(끝나면 설정에 들어감)
    pid = r["analysis"]["id"]
    run = L._running
    if run:
        run["thread"].join(30)
    assert L.summary(L.get("sfx", pid))["used"]
    assert L.file_path("sfx", pid, key="a.wav") == src / "a.wav"
    assert L.file_path("sfx", pid, out="pop_01").name == "pop_01.wav"
    assert L.file_path("sfx", pid, out="_sheet").is_file()
    for bad in ({"key": "../x.wav"}, {"key": "readme.txt"}, {"out": "../../manifest"}):
        with pytest.raises(L.LibraryError):
            L.file_path("sfx", pid, **bad)
    with pytest.raises(L.LibraryError):
        L.pack_dir("sfx", "../x")
    pack = Path(L.summary(L.get("sfx", pid))["out_dir"])
    with pytest.raises(L.LibraryError):               # 만든 꾸러미 폴더를 다시 분석하지 않음
        L.start("sfx", str(pack))
    # manifest 가 있는 폴더(사람이 정리한 꾸러미)는 분석 없이 바로 넣는다
    r2 = L.add("sfx", str(pack))
    assert r2["added"]
    L.delete("sfx", pid)
    assert not pack.exists() and settings.sfx_dirs() == [] and (src / "a.wav").is_file()
    with pytest.raises(L.LibraryError):
        L.start("sfx", str(tmp_path / "없는 폴더"))
    empty = tmp_path / "빈 폴더"
    empty.mkdir()
    with pytest.raises(L.LibraryError):
        L.start("meme", str(empty))


def test_similar_sounds_are_dropped_but_different_tones_are_not(tmp_path):
    d = tmp_path / "s"
    tone(d / "faaah.wav", dur=0.8, freq=700)
    tone(d / "fahhh_x1.wav", dur=0.8, freq=700, lead=0.05, amp=0.4)       # 같은 소리를 다시 올린 것(앞 무음·크기만 다름)
    tone(d / "faah_long.wav", dur=2.5, freq=700)                          # 꼬리를 길게 자른 사본
    tone(d / "other.wav", dur=0.8, freq=760)                              # 반음 남짓 다른 소리
    ear = FakeEar({s: {"desc": "아", "quality": 4, "fit": {"scream": 3}} for s in ("faaah", "fahhh_x1", "faah_long", "other")})
    s = L.start("sfx", str(d), llm=ear, wait=True)
    why = {it["key"]: it["auto"]["why"] for it in L.detail(L.get("sfx", s["id"]))["items"]}
    assert why["fahhh_x1.wav"] == ["비슷한 소리(faaah.wav)"] and why["faaah.wav"] == [] and why["other.wav"] == []
    assert why["faah_long.wav"] == ["비슷한 소리(faaah.wav)"]


def test_named_download_still_goes_through_drop_rules(tmp_path):
    d = tmp_path / "s"
    tone(d / "sad-meow-song.wav", dur=2.0)
    tone(d / "pop_01.wav", freq=1500)
    ear = FakeEar({"sad-meow-song": {"desc": "노래", "music": True, "source": "song", "quality": 4, "fit": {"sad": 3}},
                   "pop_01": {"desc": "톡", "quality": 2, "fit": {}}})
    s = L.start("sfx", str(d), llm=ear, wait=True)
    by = {it["key"]: it for it in L.detail(L.get("sfx", s["id"]))["items"]}
    assert by["sad-meow-song.wav"]["auto"]["why"][0] == "음악"
    assert by["pop_01.wav"]["auto"]["why"] == ["품질"]


def test_trim_and_features_on_silence():
    x = np.zeros(48000, dtype=np.float32)
    assert AS.features(x)["error"] == "소리가 거의 없음"
    assert AS.features(np.zeros(0, dtype=np.float32))["error"]
    assert AM.name_from_file("물음표_으쓱_개구리 (2).png") == "물음표 으쓱 개구리"


def test_blank_or_missing_folder_is_refused_with_what_was_typed(tmp_path):
    """빈 칸은 설치 폴더 전체를 분석하지 않고 거절한다. 없는 상대 경로는 친 그대로 알린다(설치 폴더 절대 경로를 보이지 않게)."""
    for blank in ("", "  ", '""'):
        with pytest.raises(L.LibraryError, match="폴더를 넣으세요"):
            L.add("sfx", blank)
        with pytest.raises(L.LibraryError, match="폴더를 넣으세요"):
            L.start("meme", blank)
    with pytest.raises(L.LibraryError) as e:
        L.add("sfx", "없는상대폴더")
    assert str(e.value) == "폴더가 없습니다: 없는상대폴더"
    assert L.list_all()["packs"] == []


def test_flat_pictures_are_not_taken_as_the_same_picture(tmp_path):
    """단색 그림은 차이 해시가 전부 0 이라 색이 달라도 '같은 그림'으로 빠졌다."""
    from PIL import Image
    d = tmp_path / "단색"
    d.mkdir()
    for name, c in (("초록", (30, 200, 30)), ("보라", (120, 30, 200)), ("주황", (240, 140, 20))):
        Image.new("RGB", (200, 200), c).save(d / f"{name}.png")
    s = L.start("meme", str(d), wait=True)
    assert s["counts"]["kept"] == 3


def test_edits_to_two_pictures_at_once_both_stay(tmp_path):
    """화면은 카드마다 따로 저장해 요청이 겹칠 수 있다 — 겹쳐도 변경이 모두 남는다(잠금이 없을 때는 앞의 것이 사라졌다)."""
    import threading
    d = tmp_path / "많은 짤"
    for i in range(12):
        img(d / f"p{i:02d}.png", color=(20 * i, 30, 200 - 10 * i), mark=i * 4)
    s = L.start("meme", str(d), wait=True)
    keys = [f"p{i:02d}.png" for i in range(6)]
    errs = []

    def edit(k):
        try:
            L.apply_changes("meme", s["id"], [{"key": k, "keep": False}])
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    ts = [threading.Thread(target=edit, args=(k,)) for k in keys]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errs
    by = {it["key"]: it for it in L.get("meme", s["id"])["items"]}
    assert all(by[k]["user"].get("keep") is False for k in keys)


def test_memes_with_the_same_file_name_do_not_overwrite_each_other(tmp_path):
    """꾸러미마다 m_001.png 가 있다 — 작업 폴더 사본이 이름만으로 정해지면 서로 덮어써 다른 그림이 나왔다."""
    from PIL import Image
    a, b = tmp_path / "A" / "m_001.png", tmp_path / "B" / "m_001.png"
    for p, c in ((a, (220, 20, 20)), (b, (20, 20, 220))):
        p.parent.mkdir(parents=True)
        Image.new("RGB", (64, 64), c).save(p)
    job = tmp_path / "job"
    ea, eb = {"file": str(a)}, {"file": str(b)}
    A._copy_into_job(ea, job)
    A._copy_into_job(eb, job)
    assert ea["file"] != eb["file"] and ea["file"].startswith("assets/meme/") and ea["file"].endswith("_m_001.png")
    assert Image.open(job / ea["file"]).getpixel((0, 0))[:3] == (220, 20, 20)
    assert Image.open(job / eb["file"]).getpixel((0, 0))[:3] == (20, 20, 220)
