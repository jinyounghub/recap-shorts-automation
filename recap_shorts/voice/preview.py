"""기존 로컬 음성으로 짧은 미리듣기를 만든다. 클라우드 TTS 는 사용하지 않는다."""
from __future__ import annotations

import hashlib
from pathlib import Path

from recap_shorts import settings

DEFAULT_TEXT = "요즘 커뮤니티에서 난리 난 이야기, 삼십 초 만에 정리해 줄게."
MAX_CHARS = 120


class PreviewError(RuntimeError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def make(work_root: Path, engine: str | None = None, model: str | None = None, voice: str | None = None,
         sid: int | None = None, text: str | None = None) -> tuple[Path, bool]:
    """(wav 경로, 캐시 사용 여부). 모델/목소리는 기존 로컬 실행 명령의 설정을 따른다."""
    text = " ".join((text or "").split())[:MAX_CHARS] or DEFAULT_TEXT
    engine = engine or settings.voice_engine()
    if model is not None or voice is not None:
        raise PreviewError("모델·목소리는 기존 로컬 TTS 실행 명령에서 설정하세요")
    if engine == "supertonic":
        from recap_shorts import modelfiles
        if not modelfiles.ok("supertonic"):
            raise PreviewError("기존 Supertonic 모델을 RECAP_SUPERTONIC_DIR 로 연결하세요", 409)
        sid = settings.supertonic_sid() if sid is None else sid
        if isinstance(sid, bool) or not isinstance(sid, int) or sid not in range(10):
            raise PreviewError("목소리 번호는 0~9 입니다")
        key = f"supertonic|{sid}|{modelfiles.model_dir('supertonic')}|{text}"
    elif engine == "qwen":
        from . import qwen
        qwen.configured_command()
        key = f"qwen|{qwen.configuration_key()}|{qwen.DEFAULT_INSTRUCT}|{text}"
    else:
        raise PreviewError("로컬 목소리 엔진(qwen/supertonic)만 지원합니다")
    out = Path(work_root) / "_preview" / (hashlib.sha256(key.encode("utf-8")).hexdigest()[:24] + ".wav")
    if out.is_file() and out.stat().st_size > 44:
        return out, True
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".part.wav")
    try:
        if engine == "supertonic":
            import soundfile as sf
            from . import tts
            x, sr = tts.synth_one(text, sid=sid)
            sf.write(str(tmp), x, sr, subtype="PCM_16")
        else:
            from . import qwen
            from .spoken import to_spoken
            qwen.run_cli(to_spoken(text), qwen.DEFAULT_INSTRUCT, qwen.DEFAULT_SEED, tmp)
        tmp.replace(out)
    finally:
        tmp.unlink(missing_ok=True)
    return out, False
