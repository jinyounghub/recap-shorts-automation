"""Local topic/source and legacy model settings survive the CLI handoff."""
import json
from types import SimpleNamespace

import pytest

from recap_shorts import cli, settings
from recap_shorts.collect import made
from recap_shorts.pipeline import JobStore, make_description, post_label


@pytest.mark.parametrize("source_label", [None, "내가 작성한 취재 메모"])
def test_local_topic_keeps_attribution_after_reload(tmp_path, capsys, source_label):
    topic = {"topic": "맥미니 로컬 제작", "source_text": "사용자가 제공한 자료입니다.",
             "source_url": "https://example.test/my-source"}
    if source_label:
        topic["source_label"] = source_label
    topic_path = tmp_path / "input.json"
    topic_path.write_text(json.dumps(topic, ensure_ascii=False), encoding="utf-8")
    root = tmp_path / "work"
    assert cli.main(["--work-root", str(root), "create", "--topic-file", str(topic_path)]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["source"]["site"] == "local"

    job = JobStore(root).get(created["id"])
    assert job.state["source"]["site"] == "local"
    assert post_label(job.state["source"]) == "사용자 제공 로컬 자료"
    assert made.build_index(root).jobs[0].site == "local"
    (job.dir / "script_final").mkdir()
    (job.dir / "script_final" / "script.json").write_text(
        json.dumps({"title_lines": ["완성된 영상 제목"]}), encoding="utf-8")
    (job.dir / "shot_plan.json").write_text('{"plan": {}}', encoding="utf-8")
    description = make_description(job)
    assert (source_label or "사용자 제공 로컬 자료") in description
    assert topic["topic"] in description and topic["source_url"] in description
    assert "디시인사이드" not in description and "커뮤니티 게시글" not in description


@pytest.mark.parametrize("legacy_source", ["env", "file"])
def test_legacy_gemini_model_uses_codex_default_consistently(monkeypatch, legacy_source):
    from recap_shorts.llm import codex, make_script_llm
    if legacy_source == "env":
        monkeypatch.setenv("RECAP_SCRIPT_MODEL", "gemini-3.8-flash")
    else:
        settings.path().write_text(json.dumps({"models": {"script": "gemini-3.8-flash"}}), encoding="utf-8")
    monkeypatch.setattr(codex, "CodexLLM", lambda model: SimpleNamespace(model=model))
    assert settings.model("script") == ""
    assert make_script_llm().model is None
    assert make_script_llm(model="explicit-codex-model").model == "explicit-codex-model"
