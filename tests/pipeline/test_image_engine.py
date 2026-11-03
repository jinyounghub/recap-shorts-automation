"""Native imagegen handoff validates actual files without an HTTP fallback."""
import json
from pathlib import Path

import pytest
from PIL import Image

from recap_shorts.images import codex_imagegen as native, provider


def png(path, color=(80, 120, 160), size=(640, 480)):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)


def test_missing_file_creates_stable_task_without_network(tmp_path, monkeypatch):
    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **kw: pytest.fail("network image fallback"))
    monkeypatch.setenv("RECAP_GEMINI_API_KEY", "obsolete-key-must-not-be-read")
    monkeypatch.setenv("RECAP_IMAGE_FALLBACK", "gemini")
    out = tmp_path / "assets" / "ai" / "ai_01.png"
    result = provider.generate("A Korean street", out)
    assert not result["ok"] and result["pending"]
    assert not out.exists()
    path = Path(result["manifest"])
    before = path.read_bytes()
    again = provider.generate("A Korean street", out)
    assert again["pending"] and path.read_bytes() == before
    task = json.loads(before)["tasks"][0]
    assert task["output"] == "ai/ai_01.png"
    assert task["prompt"] == "A Korean street"
    assert task["prompt_sha256"] == native.digest(task["prompt"])
    assert provider.engine_status()[1] == "codex"


def test_resume_accepts_decodable_native_file(tmp_path):
    out = tmp_path / "a.png"
    provider.generate("first scene", out)
    png(out)
    result = provider.generate("first scene", out)
    assert result["ok"] and not result["pending"]
    assert result["size"] == [640, 480]
    assert result["sha256"] == native.file_digest(out)
    assert native.current("first scene", out)
    png(out, color=(50, 60, 70))
    assert not native.current("first scene", out)
    assert provider.generate("first scene", out)["sha256"] == native.file_digest(out)
    assert native.current("first scene", out)


def test_changed_prompt_rejects_previous_bytes(tmp_path):
    out = tmp_path / "a.png"
    provider.generate("first scene", out)
    png(out)
    assert provider.generate("first scene", out)["ok"]
    stale = provider.generate("different scene", out)
    assert stale["status"] == "stale" and stale["pending"]
    assert provider.generate("different scene", out)["status"] == "stale"
    png(out, color=(200, 80, 20))
    assert provider.generate("different scene", out)["ok"]
    assert not native.current("first scene", out)
    assert native.current("different scene", out)


def test_preexisting_unregistered_image_is_not_claimed_as_generated(tmp_path):
    out = tmp_path / "old.png"
    png(out)
    assert provider.generate("new native scene", out)["status"] == "stale"


@pytest.mark.parametrize("kind", ["corrupt", "small", "portrait", "jpeg"])
def test_invalid_image_keeps_task_pending(tmp_path, kind):
    out = tmp_path / "a.png"
    provider.generate("scene", out)
    if kind == "corrupt":
        out.write_bytes(b"not an image")
    elif kind == "small":
        png(out, size=(32, 24))
    elif kind == "portrait":
        png(out, size=(480, 640))
    else:
        Image.new("RGB", (640, 480)).save(out, format="JPEG")
    result = provider.generate("scene", out)
    assert result["pending"] and result["status"] == "invalid"
    assert not native.current("scene", out)


def test_parallel_registration_preserves_all_tasks(tmp_path):
    items = [{"prompt": f"scene {i}", "out": tmp_path / "assets" / "ai" / f"ai_{i}.png"} for i in range(5)]
    results = provider.generate_many(items)
    assert all(r["pending"] for r in results)
    doc = json.loads(Path(results[0]["manifest"]).read_text())
    assert len(doc["tasks"]) == 5
    native.retain_outputs(Path(results[0]["manifest"]), [items[0]["out"]])
    assert len(json.loads(Path(results[0]["manifest"]).read_text())["tasks"]) == 1


def test_off_creates_no_task_or_file(tmp_path, monkeypatch):
    monkeypatch.setenv("RECAP_IMAGE_ENGINE", "off")
    out = tmp_path / "a.png"
    result = provider.generate("scene", out)
    assert not result["ok"] and not result["pending"]
    assert not provider.available()
    assert not out.exists() and not native.manifest_path(out).exists()
