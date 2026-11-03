"""The public headless entrypoint works without a GUI or live model calls."""
import json
from pathlib import Path
import pytest
from recap_shorts import cli


def invoke(capsys, root, *args):
    rc = cli.main(["--work-root", str(root), *args])
    return rc, json.loads(capsys.readouterr().out)


def test_create_url_is_local_then_status_keeps_running(tmp_path, capsys):
    rc, job = invoke(capsys, tmp_path, "create", "--url",
                     "https://gall.dcinside.com/mgallery/board/view/?id=test&no=42")
    assert rc == 0 and job["status"] == "ready"
    assert job["source"]["gallery"] == "test" and job["source"]["minor"]
    state = Path(job["directory"]) / "state.json"
    data = json.loads(state.read_text())
    data["status"] = "running"
    state.write_text(json.dumps(data))
    rc, current = invoke(capsys, tmp_path, "status", job["id"])
    assert rc == 0 and current["status"] == "running"


def test_topic_input_skips_collection_only(tmp_path, capsys):
    topic = tmp_path / "topic.json"
    topic.write_text(json.dumps({"topic": "주제", "source_text": "사용자가 제공한 글입니다"}))
    rc, job = invoke(capsys, tmp_path / "work", "create", "--topic-file", str(topic), "--auto", "--no-asr")
    assert rc == 0
    states = {s["key"]: s["status"] for s in job["steps"]}
    assert states["source"] == states["read"] == "done"
    assert states["script"] == "todo"
    assert job["options"]["check_asr"] is False
    assert (Path(job["directory"]) / "topic.json").is_file()


def test_read_only_image_handoff_and_path_validation(tmp_path, capsys):
    _, job = invoke(capsys, tmp_path, "create", "--url", "https://gall.dcinside.com/board/view/?id=x&no=1")
    rc, result = invoke(capsys, tmp_path, "image-tasks", job["id"])
    assert rc == 0 and result["tasks"] == []
    rc, result = invoke(capsys, tmp_path, "status", "../external")
    assert rc == 1 and result["status"] == "error"


def test_waiting_confirmation_does_not_advance_without_request(tmp_path, capsys):
    from recap_shorts.pipeline import JobStore
    job = JobStore(tmp_path).create({"gallery": "test", "no": "1"})
    job.update_step("source", status="done")
    job.update_step("read", status="waiting")
    job.set_status("waiting")
    rc, current = invoke(capsys, tmp_path, "resume", job.id)
    assert rc == cli.PAUSED
    assert next(s for s in current["steps"] if s["key"] == "read")["status"] == "waiting"


def test_work_lock_prevents_parallel_writers(tmp_path):
    with cli.work_lock(tmp_path):
        with pytest.raises(RuntimeError, match="다른 제작"):
            with cli.work_lock(tmp_path):
                raise AssertionError("second writer entered")
    with cli.work_lock(tmp_path):
        pass


def test_browser_is_headless_and_portable(monkeypatch):
    from recap_shorts.collect.browser import edge_browser
    import playwright.sync_api
    calls = []
    class Browser:
        def close(self):
            calls.append("closed")
    class Chromium:
        def launch(self, **kwargs):
            calls.append(kwargs)
            return Browser()
    class Manager:
        chromium = Chromium()
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    monkeypatch.setattr(playwright.sync_api, "sync_playwright", Manager)
    monkeypatch.delenv("RECAP_BROWSER_PATH", raising=False)
    monkeypatch.delenv("RECAP_BROWSER_CHANNEL", raising=False)
    with edge_browser():
        pass
    assert calls == [{"headless": True}, "closed"]
