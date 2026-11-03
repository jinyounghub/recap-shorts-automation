"""기존 PyTorch Qwen3-TTS VoiceDesign 모델을 로컬에서 실행하는 선택적 어댑터.

    /path/to/existing/venv/bin/python tools/qwen_tts/synth_cli.py request.json

RECAP_QWEN_MODEL 은 기존 로컬 모델 폴더 또는 이미 캐시된 모델 ID 이다.
새 모델을 받거나 HF_HOME/기존 캐시를 바꾸지 않는다. MLX/Base/참조 음성 기반
기존 실행기는 RECAP_QWEN_COMMAND_JSON 또는 RECAP_QWEN_SCRIPT 로 그대로 연결한다.
이 파일은 VoiceDesign 전용이다. 다른 모델 종류를 VoiceDesign 으로 바꾸지 않는다.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def select_device(torch, requested: str = "auto") -> str:
    """Mac MPS, NVIDIA CUDA, CPU 중 현재 런타임에서 실제 가능한 장치만 선택."""
    if requested not in {"auto", "cpu", "mps", "cuda", "cuda:0"}:
        raise ValueError("RECAP_QWEN_DEVICE 는 auto/cpu/mps/cuda/cuda:0 중 하나여야 합니다")
    cuda = bool(torch.cuda.is_available())
    mps_backend = getattr(getattr(torch, "backends", None), "mps", None)
    mps = bool(mps_backend is not None and mps_backend.is_available())
    if requested == "auto":
        return "cuda:0" if cuda else "mps" if mps else "cpu"
    if requested.startswith("cuda") and not cuda:
        raise ValueError("요청한 CUDA 장치를 사용할 수 없습니다")
    if requested == "mps" and not mps:
        raise ValueError("요청한 MPS 장치를 사용할 수 없습니다")
    return "cuda:0" if requested == "cuda" else requested


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("사용법: synth_cli.py <입력.json>", file=sys.stderr)
        return 2
    req = json.loads(Path(argv[0]).expanduser().read_text(encoding="utf-8"))
    model_name = req.get("model") or os.environ.get("RECAP_QWEN_MODEL", "").strip()
    if not model_name:
        print("RECAP_QWEN_MODEL 에 기존 VoiceDesign 모델의 경로/캐시 ID 를 지정하세요. "
              "MLX/Base 모델은 기존 실행 명령을 RECAP_QWEN_COMMAND_JSON 에 연결하세요.", file=sys.stderr)
        return 2
    model_name = os.path.expanduser(model_name)
    # Enforce offline model lookup; model/tokenizer loading may spawn nested hub calls.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    real_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        import numpy as np
        import soundfile as sf
        import torch
        from qwen_tts import Qwen3TTSModel

        device = select_device(torch, os.environ.get("RECAP_QWEN_DEVICE", "auto"))
        dtype = torch.bfloat16 if device.startswith("cuda") else torch.float32
        t0 = time.monotonic()
        model = Qwen3TTSModel.from_pretrained(
            model_name, device_map=device, dtype=dtype, local_files_only=True,
            attn_implementation="sdpa",
        )
        load_sec = time.monotonic() - t0
        seed = int(req.get("seed", 0))
        torch.manual_seed(seed)
        if device.startswith("cuda"):
            torch.cuda.manual_seed_all(seed)
            torch.cuda.reset_peak_memory_stats()
        gen = {}
        if req.get("max_new_tokens") is not None:
            gen["max_new_tokens"] = int(req["max_new_tokens"])
        if req.get("temperature") is not None:
            gen["temperature"] = float(req["temperature"])
        t1 = time.monotonic()
        wavs, sr = model.generate_voice_design(
            text=req["text"], instruct=req.get("instruct", ""),
            language=req.get("language", "Korean"), **gen,
        )
        if device.startswith("cuda"):
            torch.cuda.synchronize()
        elif device == "mps":
            torch.mps.synchronize()
        synth_sec = time.monotonic() - t1
        x = np.asarray(wavs[0], dtype=np.float32)
        if x.size == 0 or not np.isfinite(x).all() or int(sr) <= 0:
            raise ValueError("Qwen 출력이 비어 있거나 유효한 오디오가 아닙니다")
        out = Path(req["out"]).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(out), x, sr, subtype="PCM_16")
        cuda = device.startswith("cuda")
        res = {
            "out": str(out), "duration": round(len(x) / sr, 3), "sample_rate": int(sr),
            "load_sec": round(load_sec, 2), "synth_sec": round(synth_sec, 2),
            "device": device, "gpu": device != "cpu",
            "gpu_name": torch.cuda.get_device_name(0) if cuda else "Apple MPS" if device == "mps" else None,
            "gpu_mem_peak_mb": round(torch.cuda.max_memory_allocated() / 2**20) if cuda else None,
            "seed": seed, "model": model_name,
        }
    finally:
        sys.stdout = real_stdout
    print(json.dumps(res, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
