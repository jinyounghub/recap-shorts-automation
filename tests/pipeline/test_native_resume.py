"""Integration contracts for the Codex-agent image handoff and synchronous CLI."""
import json
from pathlib import Path

from recap_shorts import pipeline as P
from recap_shorts.images import provider
from tests.pipeline.fakes import FakeDeps
from tests.pipeline.test_image_engine import png


class NativeDeps(FakeDeps):
    def generate_image(self, prompt, out_png):
        self.hit("generate_image")
        return provider.generate(prompt, out_png)

    def hint_prompts(self, hints):
        self.hit("hint_prompts")
        # Preserve the input identity so a changed script changes the prompt.
        return ["Scene: " + hint for hint in hints]


def create(tmp_path):
    deps = NativeDeps()
    store = P.JobStore(tmp_path)
    job = store.create({"gallery": "dcbest", "no": 2101}, {
        "auto_steps": list(P.CONFIRM_STEPS), "sensitive_treatment": False,
        "entity_images": False, "image_quality_check": False,
    })
    runner = P.Runner(store, deps)
    runner.run_job(job)
    return deps, store, runner, job


def fill(job, only=None):
    manifest = job.dir / job.state["artifacts"]["imagegen_tasks"]
    tasks = json.loads(manifest.read_text())["tasks"]
    for i, task in enumerate(tasks):
        if only is not None and i not in only:
            continue
        png(manifest.parent / task["output"], color=(20 + i * 25, 90, 180))
    return manifest, tasks


def test_native_images_pause_even_with_usable_captures_and_auto(tmp_path):
    deps, store, runner, job = create(tmp_path)
    assert job.state["status"] == "awaiting_images", job.state["log"]
    assert job.step("assets")["status"] == "awaiting_images"
    assert job.step("voice")["status"] == "done"
    assert job.step("scenes")["status"] == "todo"
    assert any(e["kind"] == "cap" for e in P.load_assets(job))
    assert not (job.dir / "out/final.mp4").exists()
    assert not deps.calls.get("render")
    counts = dict(deps.calls)
    manifest = job.dir / job.state["artifacts"]["imagegen_tasks"]
    before = manifest.read_bytes()
    runner.run_job(job)
    assert job.state["status"] == "awaiting_images"
    assert manifest.read_bytes() == before
    for name in ("get_post", "make_voice", "hint_prompts", "locate_regions"):
        assert deps.calls[name] == counts[name]


def test_partial_then_complete_resume_survives_new_process(tmp_path):
    deps, store, runner, job = create(tmp_path)
    manifest, tasks = fill(job, only={0})
    assert len(tasks) > 1
    runner.run_job(job)
    assert job.state["status"] == "awaiting_images"
    assert json.loads(manifest.read_text())["tasks"][0]["status"] == "complete"
    first_sha = (manifest.parent / tasks[0]["output"]).read_bytes()
    fill(job)
    second = P.JobStore(tmp_path).get(job.id)
    assert second.state["status"] == "awaiting_images"
    P.Runner(P.JobStore(tmp_path), deps).run_job(second)
    assert second.state["status"] == "done", second.state["log"]
    assert deps.calls["get_post"] == 1 and deps.calls["make_voice"] == 1
    assert deps.calls["hint_prompts"] == 1
    assert deps.calls["render"] == 1
    assert (manifest.parent / tasks[0]["output"]).read_bytes() == first_sha
    description = (second.dir / "out/description.txt").read_text()
    assert "로컬 모델" in description and "Gemini TTS" not in description


def test_changed_scene_requires_replacement_instead_of_accepting_old_image(tmp_path):
    deps, store, runner, job = create(tmp_path)
    manifest, tasks = fill(job)
    runner.run_job(job)
    assert job.state["status"] == "done"
    script_path = job.dir / "script_final/script.json"
    script = json.loads(script_path.read_text())
    script["sentences"][0]["image_hint"] = "장면 변경: 비 오는 한국 운동장"
    script_path.write_text(json.dumps(script, ensure_ascii=False))
    job.reset_from("assets")
    runner.run_job(job)
    assert job.state["status"] == "awaiting_images"
    doc = json.loads(manifest.read_text())
    stale = [t for t in doc["tasks"] if t["status"] == "stale"]
    assert len(stale) == 1 and stale[0]["output"] == "ai/ai_01.png"
    png(manifest.parent / stale[0]["output"], color=(201, 202, 203))
    runner.run_job(job)
    assert job.state["status"] == "done"
    assert deps.calls["make_voice"] == 1


def test_repeated_create_never_overwrites_existing_job(tmp_path):
    store = P.JobStore(tmp_path)
    first = store.create({"gallery": "dcbest", "no": 1})
    (first.dir / "keep.txt").write_text("keep")
    second = store.create({"gallery": "dcbest", "no": 1})
    assert first.id != second.id and first.dir != second.dir
    assert (first.dir / "keep.txt").read_text() == "keep"
