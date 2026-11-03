"""단계가 예외로 멈출 때: 화면(status=error) 문구에는 예외 이름·Traceback·경로가 없고, 자세한 기록은 작업 폴더 _logs/errors.log 에만."""
import json
import re

from recap_shorts import pipeline as P
from tests.pipeline.fakes import FakeDeps

SRC = {"gallery": "baseball_new13", "no": 2101,
       "url": "https://gall.dcinside.com/board/view/?id=baseball_new13&no=2101", "minor": False}
LEAK = re.compile(r"[A-Za-z]:\\|Traceback|File \"|RuntimeError|CodexError")


class WrongAuthDeps(FakeDeps):
    """Codex ChatGPT 인증 실패는 첫 이미지에서 멈춰 로그인 문제를 표시한다."""

    def __init__(self):
        super().__init__()
        self.reads = 0

    def read_image(self, images, instruction):
        self.reads += 1
        from recap_shorts import errors
        from recap_shorts.llm.codex import CodexError
        raise errors.mark(CodexError("Codex ChatGPT 인증 실패"), errors.BAD_KEY)



def run(tmp_path, deps, options=None):
    store = P.JobStore(tmp_path)
    runner = P.Runner(store, deps)
    runner.start()
    job = store.create(dict(SRC), options or {"auto_steps": ["read"]})
    runner.submit(job)
    assert runner.wait_idle(60)
    return job


def test_auth_failure_stops_at_first_image_and_says_so(tmp_path):
    deps = WrongAuthDeps()
    job = run(tmp_path, deps)
    st = job.step("read")
    assert job.state["status"] == "error" and st["status"] == "error"
    assert deps.reads == 1                                        # 그림 8장을 같은 실패로 되풀이하지 않는다
    assert "Codex ChatGPT 인증" in st["error"] and "codex login" in st["error"]
    assert "_logs/errors.log" in st["error"]                      # 자세한 기록 위치는 알려 준다
    assert not LEAK.search(st["error"]), st["error"]
    assert not LEAK.search(json.dumps(job.state["log"], ensure_ascii=False))   # 제작 기록에도 없다
    assert st["sub"][0]["status"] == "error" and "Codex ChatGPT 인증" in st["sub"][0]["note"]
    log = (job.dir / "_logs" / "errors.log").read_text(encoding="utf-8")
    assert "Traceback (most recent call last)" in log and "CodexError" in log   # 파일에는 남는다


def test_step_error_with_plain_exception_keeps_korean_message(tmp_path):
    deps = FakeDeps()
    deps.fail_once.add("make_voice")
    job = run(tmp_path, deps, {"auto_steps": ["read", "script"]})
    st = job.step("voice")
    assert job.state["status"] == "error" and "일부러 실패" in st["error"]
    assert not LEAK.search(st["error"]) and "_logs/errors.log" in st["error"]
    assert (job.dir / "_logs" / "errors.log").is_file()


def test_read_failure_without_key_text_keeps_old_message(tmp_path):
    """키 문제가 아닌 읽기 실패는 예전처럼 '글자를 하나도 읽지 못했습니다'로 모은다."""
    deps = FakeDeps()

    def boom(images, instruction):
        raise RuntimeError("모델이 이상한 답을 냄")
    deps.read_image = boom
    job = run(tmp_path, deps)
    st = job.step("read")
    assert st["status"] == "error" and "글자를 하나도 읽지 못했습니다" in st["error"]
