"""효과음 폴더 설정(settings.sfx_dirs) + manifest 없는 폴더 읽기(fx/sfx.scan_folder) + 고르기(fx/apply.choose_sfx_library).
설정 파일은 시험마다 빈 임시 파일(tests/conftest.py)."""
import json
import math
import struct
import wave
from pathlib import Path

import pytest

from recap_shorts import settings
from recap_shorts.fx import apply as A
from recap_shorts.fx import rights as R
from recap_shorts.fx import sfx as S
from recap_shorts.fx.config import ROOT, load_config


def write_wav(path, dur=0.25, sr=48000):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"".join(struct.pack("<h", int(0.8 * 32767 * math.sin(2 * math.pi * 800 * i / sr)))
                               for i in range(int(sr * dur))))


def plain_folder(tmp_path, names, name="mine"):
    d = tmp_path / name
    for n in names:
        write_wav(d / n)
    d.mkdir(parents=True, exist_ok=True)
    return d


def manifest_folder(tmp_path, types, name="base"):
    d = tmp_path / name
    items = []
    for t in types:
        write_wav(d / f"{t}_01.wav")
        items.append({"id": f"{t}_01", "file": f"{t}_01.wav", "type": t, "duration": 0.25})
    (d / "manifest.json").write_text(json.dumps({"version": 1, "items": items}), encoding="utf-8")
    return d


@pytest.mark.parametrize("rel, want", [
    ("pop_01.wav", "pop"), ("boom.wav", "boom"), ("whoosh_short-2.mp3", "whoosh_short"), ("pop_cartoon.wav", "pop"),
    ("Ding (3).wav", "ding"), ("pop/아무이름.mp3", "pop"), ("laugh/x/y.wav", "laugh"), ("vine-boom.mp3", None),
    ("효과음.wav", None),
])
def test_type_from_name(rel, want):
    assert S.type_from_name(Path(rel)) == want


def test_folder_without_manifest_reads_names_and_subfolders(tmp_path):
    d = plain_folder(tmp_path, ["pop_01.wav", "boom.wav", "huh/what.wav", "vine-boom.wav", "readme.txt"])
    lib, notes = S.load_library([d])
    assert not notes
    assert sorted((it["id"], it["type"]) for it in lib) == [("boom", "boom"), ("huh/what", "huh"), ("pop_01", "pop")]
    assert all(Path(it["file"]).is_file() and it["duration"] == pytest.approx(0.25, abs=0.01) for it in lib)
    items, skipped, has_mf = S.folder_items(d, durations=False)
    assert skipped == 1 and not has_mf and "duration" not in items[0]   # vine-boom 만 모름(txt 는 소리 파일이 아님)


def test_manifest_folder_still_wins_over_names(tmp_path):
    d = manifest_folder(tmp_path, ["sparkle"])
    write_wav(d / "pop_09.wav")                     # manifest 에 없는 파일은 이름이 맞아도 쓰지 않는다
    lib, _ = S.load_library([d])
    assert [it["id"] for it in lib] == ["sparkle_01"]


def test_save_and_view_sfx_dirs(tmp_path):
    d = plain_folder(tmp_path, ["pop_01.wav", "boom_01.wav"])
    v = settings.save(sfx={"dirs": [str(d), str(d)]})
    assert settings.sfx_dirs() == [str(d.resolve())]            # 같은 폴더는 한 번
    assert v["sfx"]["using"] == "custom" and v["sfx"]["dirs"][0]["count"] == 2
    assert v["sfx"]["dirs"][0]["types"] == {"boom": 1, "pop": 1} and v["sfx"]["fill_missing"] is True
    settings.save(sfx={"fill_missing": False})
    assert settings.sfx_fill_missing() is False and settings.sfx_dirs() == [str(d.resolve())]   # 준 것만 바꾼다
    settings.save(image_engine="codex")
    assert settings.sfx_dirs() == [str(d.resolve())]            # 다른 제작 설정을 저장해도 효과음 설정은 남는다
    v = settings.save(sfx={"dirs": []})
    assert settings.sfx_dirs() == [] and v["sfx"]["using"] == "default"
    assert [f["path"] for f in v["sfx"]["default"]] == load_config()["sfx"]["libraries"]


def test_save_rejects_bad_folders(tmp_path):
    with pytest.raises(settings.SettingsError, match="폴더가 없습니다"):
        settings.save(sfx={"dirs": [str(tmp_path / "none")]})
    empty = plain_folder(tmp_path, ["vine-boom.wav"], name="empty")
    with pytest.raises(settings.SettingsError, match="종류_번호"):
        settings.save(sfx={"dirs": [str(empty)]})
    with pytest.raises(settings.SettingsError):
        settings.save(sfx={"dirs": "C:/x"})
    with pytest.raises(settings.SettingsError):
        settings.save(sfx={"fill_missing": "yes"})
    assert settings.sfx_dirs() == []


def test_choose_uses_custom_and_fills_missing_types(tmp_path):
    base = manifest_folder(tmp_path, ["pop", "sparkle", "boom"])
    mine = plain_folder(tmp_path, ["pop_01.wav", "pop_02.wav"])
    scfg = {"libraries": [str(base)]}
    lib, notes, used = A.choose_sfx_library(scfg)
    assert not used and {it["type"] for it in lib} == {"pop", "sparkle", "boom"}
    settings.save(sfx={"dirs": [str(mine)]})
    lib, notes, used = A.choose_sfx_library(scfg)
    pops = [it for it in lib if it["type"] == "pop"]
    assert used and all(Path(it["file"]).parent == mine.resolve() for it in pops) and len(pops) == 2
    assert {it["type"] for it in lib} == {"pop", "sparkle", "boom"}        # 없는 종류는 기본에서 채움
    assert any("기본 효과음으로 채움" in n for n in notes)
    settings.save(sfx={"fill_missing": False})
    lib, _, _ = A.choose_sfx_library(scfg)
    assert {it["type"] for it in lib} == {"pop"}


def test_choose_falls_back_when_custom_folder_is_gone(tmp_path):
    base = manifest_folder(tmp_path, ["pop"])
    mine = plain_folder(tmp_path, ["boom_01.wav"])
    settings.save(sfx={"dirs": [str(mine)]})
    (mine / "boom_01.wav").unlink()
    mine.rmdir()
    lib, notes, used = A.choose_sfx_library({"libraries": [str(base)]})
    assert not used and [it["id"] for it in lib] == ["pop_01"]
    assert any("찾지 못해 기본 효과음" in n for n in notes)
    assert settings.sfx_view()["dirs"][0]["exists"] is False


def test_apply_fx_puts_custom_folder_sounds_in_timeline(tmp_path):
    mine = plain_folder(tmp_path, ["pop_01.wav", "hit_01.wav", "sparkle_01.wav", "ding_01.wav", "boom_01.wav"])
    settings.save(sfx={"dirs": [str(mine)], "fill_missing": False})
    cfg = load_config(override={"memes": {"enabled": False}, "beats": {"use_llm": False}})
    shots = [{"file": "x", "start": i * 2.0, "end": (i + 1) * 2.0, "motion": "static"} for i in range(10)]
    tl = {"duration": 20.0, "shots": shots, "hook": {"text": "t", "start": 0.0, "end": 2.0},
          "audio": {"voice": "voice.wav"}, "captions": []}
    timings = {"sentences": [{"index": 0, "role": "hook", "text": "가", "start": 0.0, "end": 2.0},
                             {"index": 1, "role": "body", "text": "나?", "start": 2.0, "end": 4.0}], "duration": 4.0}
    new, rep = A.apply_fx(tl, timings, tmp_path, cfg)
    ev = new["audio"]["sfx"]
    assert ev and all(Path(e["file"]).parent == mine.resolve() for e in ev)
    assert any("설정한 효과음 폴더를 씀" in n for n in rep["notes"])


def test_shipped_packs_are_complete():
    """저장소에 같이 넣는 기본 꾸러미: manifest 의 파일이 다 있고, 출처 표기가 필요한 소리는 credit 이 '필수:' 로 시작."""
    for rel in load_config()["sfx"]["libraries"]:
        d = ROOT / rel
        doc = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        assert doc["items"], rel
        for it in doc["items"]:
            assert (d / it["file"]).is_file() and it["type"] in S.KNOWN_TYPES, (rel, it["id"])
            assert it.get("license"), (rel, it["id"])
            if "CC BY" in str(it["license"]):
                assert str(it.get("credit", "")).startswith("필수:"), (rel, it["id"])


def test_rights_check_covers_shipped_meme_pack():
    """저작권 점검 기록(pack=user_meme)이 저장소 기본 밈 꾸러미 폴더 이름과 맞아야 검수 화면 경고가 나온다."""
    meme = next(r for r in load_config()["sfx"]["libraries"] if r.endswith("user_meme"))
    assert Path(meme).name == R.load_rights()["pack"]


def test_fill_missing_only_adds_types_the_rules_use(tmp_path):
    """기본 꾸러미에서 채울 때 규칙(자리 설정)이 고르지 않는 종류는 넣지 않는다 — 안 쓰는 소리가 후보에 끼지 않게."""
    base = manifest_folder(tmp_path, ["pop", "boom", "error", "boing"])
    mine = plain_folder(tmp_path, ["pop_01.wav"])
    settings.save(sfx={"dirs": [str(mine)]})
    scfg = {"libraries": [str(base)], "cuts": {"types": {"pop": 0.5, "boom": 0.2}}, "type_fallback": {"coin": "pop"}}
    assert S.slot_types(scfg) == {"pop", "boom"}
    lib, notes, used = A.choose_sfx_library(scfg)
    assert used and {it["type"] for it in lib} == {"pop", "boom"}
    assert S.slot_types(load_config()["sfx"]) >= {"hit", "pop", "ding", "sparkle", "boom", "huh", "punchline"}


def test_folder_deleted_outside_the_app_does_not_block_removing_another(tmp_path):
    """폴더 A·B 를 넣고 B 를 탐색기에서 지운 뒤 A 를 빼면, 예전에는 B 때문에 '폴더가 없습니다'(절대 경로)로 막혔다."""
    import shutil
    a = plain_folder(tmp_path, ["pop_01.wav"], name="a")
    b = plain_folder(tmp_path, ["boom_01.wav"], name="b")
    settings.save(sfx={"dirs": [str(a), str(b)]})
    shutil.rmtree(b)
    settings.save(sfx={"dirs": [str(b)]})                    # A 빼기 — 없어진 B 는 그대로 남는다
    assert settings.sfx_dirs() == [str(b.resolve())] and settings.sfx_view()["dirs"][0]["exists"] is False
    settings.save(sfx={"dirs": []})
    assert settings.sfx_dirs() == []
    with pytest.raises(settings.SettingsError) as e:         # 새로 넣는 폴더는 여전히 확인한다(친 그대로 알림)
        settings.save(sfx={"dirs": ["없는상대폴더"]})
    assert str(e.value) == "폴더가 없습니다: 없는상대폴더"


def test_fallback_type_is_not_reported_as_missing(tmp_path):
    """컷 종류가 꾸러미에 없어도 type_fallback 으로 채웠으면 '없는 효과음 종류'로 알리지 않는다."""
    base = manifest_folder(tmp_path, ["sparkle", "hit", "pop"])
    cfg = load_config(override={"memes": {"enabled": False}, "beats": {"use_llm": False},
                                "sfx": {"libraries": [str(base)], "cuts": {"types": {"ding": 1.0}, "skip_ratio": 0.0}}})
    shots = [{"file": "x", "start": i * 2.0, "end": (i + 1) * 2.0, "motion": "static"} for i in range(10)]
    tl = {"duration": 20.0, "shots": shots, "hook": {"text": "t", "start": 0.0, "end": 2.0},
          "audio": {"voice": "voice.wav"}, "captions": []}
    timings = {"sentences": [{"index": 0, "role": "hook", "text": "가", "start": 0.0, "end": 2.0},
                             {"index": 1, "role": "body", "text": "나", "start": 2.0, "end": 4.0}], "duration": 4.0}
    new, rep = A.apply_fx(tl, timings, tmp_path, cfg)
    assert any(e["type"] == "sparkle" for e in new["audio"]["sfx"])
    assert not any("ding" in n for n in rep["notes"] if "없는 효과음 종류" in n)


def test_shipped_packs_cover_every_type_the_rules_pick():
    """기본 꾸러미만 있는 PC(배포판)에서도 규칙이 고르는 종류마다 소리가 있어야 한다(없으면 type_fallback 의 종류로).
    smug 짤의 sus 가 배포판에서만 조용히 빠졌다 — 개발 PC 의 꾸러미에는 sus 가 있어서 안 보였다."""
    scfg = load_config()["sfx"]
    have = set()
    for rel in scfg["libraries"]:
        have |= {it["type"] for it in json.loads((ROOT / rel / "manifest.json").read_text(encoding="utf-8"))["items"]}
    fb = scfg.get("type_fallback") or {}
    assert sorted(t for t in S.slot_types(scfg) if t not in have and fb.get(t) not in have) == []


def test_shipped_meme_pack_has_no_risky_sounds():
    """공개 저장소에 같이 넣는 밈 꾸러미에는 저작권 점검 기록에서 high·medium 인 소리를 넣지 않는다."""
    meme = next(r for r in load_config()["sfx"]["libraries"] if r.endswith("user_meme"))
    items = json.loads((ROOT / meme / "manifest.json").read_text(encoding="utf-8"))["items"]
    levels = R.load_rights()["items"]
    assert [it["id"] for it in items if (levels.get(it["id"]) or {}).get("level") in ("high", "medium")] == []
    assert sorted(p.name for p in (ROOT / meme).glob("*.wav")) == sorted(it["file"] for it in items)
