# Recap Shorts Automation — macOS + Codex

커뮤니티 글이나 사용자가 제공한 자료로 30~40초 한국어 요약 쇼츠를 만드는 **맥용 Python 제작 파이프라인**입니다. Codex가 제작을 진행하고, 음성·편집은 맥미니의 기존 로컬 환경을 사용합니다.

## 실행 구조

| 작업 | 이 포크의 실행 방식 |
|---|---|
| 대본, 자료 이미지 읽기, 장면·효과 분석 | 기존 ChatGPT 로그인으로 `codex exec` |
| 생성 이미지 | 현재 Codex 세션의 **내장 imagegen** |
| 음성 | **이미 사용 중인 Qwen/MLX 실행 명령** 또는 기존 Supertonic |
| 받아쓰기·타이밍 보정 | 기존 로컬 SenseVoice, 명시적으로 끌 수 있음 |
| 자막·효과·영상 합성 | Pillow, NumPy/SciPy, FFmpeg |
| 저장 | `work/<작업 ID>/`의 로컬 파일 |
| 조작 | `python -m recap_shorts` — 별도 GUI·웹 서버 없음 |

Gemini/Antigravity/Cloudflare 모델 호출, 외부 이미지 서버의 유료 fallback, Google Drive 보관·로컬 원본 자동 삭제, Windows GUI·설치 스크립트·CapCut 앱 연동을 제거했습니다. 기존 원문·댓글 캡처, 공개 라이선스 사진, 사실 근거 확인, 자막·효과음·렌더 기능은 유지합니다. 원본의 AI 보조 이미지 비중 기본값(`max_ai_share=0.34`)도 유지합니다.

## 인증과 imagegen

`codex exec`는 사용자가 이미 로그인한 Codex 인증을 재사용합니다. 이 프로젝트는 ChatGPT 로그인과 OpenAI Codex provider를 선택하고, 자식 프로세스에 API 결제용 키를 전달하지 않습니다. 토큰 파일을 읽거나 OAuth 토큰을 추출해 REST API에 붙이지 않습니다. 모델을 지정하지 않으면 사용자의 현재 Codex 모델 설정을 따릅니다.

이미지 생성은 Python에서 `codex exec`를 호출하면 반드시 가능한 기능이라고 가정하지 않습니다. 생성이 필요한 단계에서 다음 작업 목록을 기록합니다.

```text
work/<작업 ID>/assets/imagegen_tasks.json
```

Codex는 이 목록의 프롬프트로 **실제 내장 imagegen 도구**를 호출하고, 나온 PNG를 지정 경로에 저장한 뒤 제작을 재개합니다. 내장 도구가 없는 세션에서는 작업이 `awaiting_images`에 남습니다. 별도 유료 Images API나 가짜 이미지로 전환하지 않습니다. 이미지 생성은 사용하는 Codex 계정의 기능과 사용 한도에 따릅니다.

공식 근거: [인증](https://learn.chatgpt.com/docs/auth), [비대화형 실행](https://learn.chatgpt.com/docs/non-interactive-mode), [내장 이미지 생성](https://learn.chatgpt.com/docs/image-generation).

## 맥에서 준비하기

필요한 도구는 Python 3.11 이상, Codex CLI, FFmpeg/FFprobe와 기존 로컬 TTS 환경입니다. Qwen/MLX가 정상 작동하는 가상환경을 이 프로젝트의 의존성 설치 환경으로 사용하지 마세요.

프로젝트 전용 가상환경이 아직 없을 때:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

기존 도구 확인:

```bash
codex --version
codex login status
command -v ffmpeg
command -v ffprobe
```

ChatGPT 로그인이 없을 때만 `codex login`을 실행합니다. FFmpeg가 없다면 Homebrew의 `brew install ffmpeg`로 준비할 수 있습니다. Homebrew 경로(`/opt/homebrew/bin` 등)가 Codex가 실행하는 셸의 PATH에도 있어야 합니다.

게시글 캡처는 창을 띄우지 않는 Playwright를 사용합니다. 기존 Chrome을 연결하거나, 캡처용 Chromium이 없을 때 다음을 실행합니다.

```bash
.venv/bin/python -m playwright install chromium
```

이 명령은 브라우저를 준비하며, TTS 모델을 설치하지 않습니다.

## 기존 Qwen/MLX 연결

**모델 ID·가상환경·참조 음성·기존 음성 설정을 바꾸지 않고, 현재 작동하는 실행 명령만 연결합니다.** 실제 맥의 경로는 이 저장소에서 추측하지 않습니다.

`config/runtime.example.json`을 참고해 **개인 설정** `config/runtime.local.json`을 만드세요. 아래는 형식 예시이며, 실행 파일과 옵션은 기존 명령에 맞춰야 합니다.

```json
{
  "qwen_command": [
    "/Users/사용자/기존-tts-환경/bin/python",
    "/Users/사용자/기존-tts-wrapper.py",
    "--text-file", "{text_file}",
    "--output", "{output}"
  ],
  "sensevoice_dir": "/Users/사용자/기존-SenseVoice-모델",
  "browser_channel": "chrome"
}
```

- 명령은 셸 문자열이 아닌 **인수 배열**입니다. 경로의 공백·한글을 그대로 지원합니다.
- `{text}`, `{text_file}`, `{output}`, `{request_json}`, `{instruct}`, `{seed}`, `{language}`를 치환할 수 있습니다.
- 원래 명령의 모델 경로, 화자, 참조 음성 등은 인수 배열에 그대로 둡니다. 지원하지 않는 새 옵션을 임의로 추가하지 마세요.
- `{output}`은 생성해야 할 정확한 WAV 파일 경로입니다. 출력 접두사나 폴더만 받는 도구는 기존 도구를 부르는 작은 로컬 wrapper로 연결하세요.
- `qwen_command` 대신 기존 `qwen_python`, `qwen_script`를 지정하면 해당 스크립트에 요청 JSON 경로 하나를 넘깁니다.
- 프로젝트에 포함된 `tools/qwen_tts/synth_cli.py`는 **이미 있는 PyTorch VoiceDesign 모델**을 위한 선택적 어댑터입니다. MLX/Base/참조 음성 모델을 이 어댑터로 교체하지 않습니다.
- 파일 대신 같은 이름의 `RECAP_QWEN_*` 환경 변수를 사용해도 됩니다. 이미 설정한 환경 변수가 로컬 JSON보다 우선합니다.
- `runtime.local.json`, `settings.local.json`, 모델·작업 결과·개인 경로는 커밋하지 않습니다.

로컬 SenseVoice를 쓰려면 제작용 Python에서 기존 `sherpa_onnx` 런타임을 사용할 수 있어야 합니다. 없는 모델을 자동으로 받지 않습니다. ASR을 끌 필요가 있을 때만 작업 생성에 `--no-asr`를 지정하세요. 이 경우 음성·장면·효과의 ASR 경로를 모두 끄며 자막 타이밍은 별도로 확인해야 합니다.

자세한 연결 절차와 요청 JSON 형식은 [맥 Codex 인수인계](docs/macos_codex.md)에 있습니다.

## 제작 명령

진단은 로그인하거나 모델을 로드하지 않습니다. `ready`는 로컬 사전 점검 결과이며 실제 추론·imagegen 성공을 뜻하지 않습니다.

```bash
.venv/bin/python -m recap_shorts doctor
```

DCinside 또는 Blind 게시글로 작업을 만들고 진행:

```bash
.venv/bin/python -m recap_shorts create --url '게시글_URL' --auto --run
```

직접 정리한 자료도 입력할 수 있습니다. `topic.json` 예:

```json
{
  "topic": "영상의 주제",
  "source_kind": "user_source",
  "source_label": "내가 제공한 자료",
  "source_url": "https://원문주소",
  "source_text": "근거가 되는 실제 원문 텍스트"
}
```

```bash
.venv/bin/python -m recap_shorts create --topic-file topic.json --auto --run
```

`--topic-file`은 텍스트 입력용입니다. 파일에 원문을 넣어야 하며 첨부 캡처 수집은 `--url` 경로를 사용합니다. 로컬 자료를 디시 게시글로 잘못 표시하지 않습니다.

명령은 JSON으로 작업 ID와 상태를 출력합니다. `--auto`를 빼면 자료 읽기·대본·장면·최종 검수 준비 단계마다 `waiting`에서 멈춥니다. Codex가 결과를 확인한 뒤 `resume ID --approve`로 진행할 수 있습니다.

```bash
.venv/bin/python -m recap_shorts list
.venv/bin/python -m recap_shorts status 작업_ID
.venv/bin/python -m recap_shorts image-tasks 작업_ID
.venv/bin/python -m recap_shorts resume 작업_ID
```

### 이미지 대기와 재개

1. 상태가 `awaiting_images`이면 `image-tasks`의 작업 목록을 읽습니다.
2. Codex가 내장 imagegen으로 `pending`·`invalid`·`stale` 이미지들을 생성합니다.
3. 각 `output`은 **manifest가 있는 `assets` 폴더 기준 상대 경로**입니다. `ai/ai_01.png`이면 `work/ID/assets/ai/ai_01.png`에 실제 PNG를 저장합니다.
4. 기본 이미지 상자는 4:3입니다. 가능하면 4:3 가로 이미지로 생성하세요.
5. `resume`을 실행합니다. 존재 여부뿐 아니라 PNG 디코딩·크기·방향·프롬프트 해시·파일 해시를 확인합니다.

파일이 빠지거나 손상됐으면 렌더링으로 넘어가지 않습니다. 완료한 원문 수집·대본·음성은 재사용합니다. 같은 작업을 반복 재개해도 장면 프롬프트 번역과 준비된 캡처 분석을 반복하지 않습니다. `--approve`나 `--auto`도 빠진 이미지를 완료 처리하지 못합니다.

| 종료 코드 | 의미 |
|---|---|
| `0` | 명령 성공 또는 제작 완료 |
| `1` | 설정·도구·제작 오류. JSON과 작업 로그 확인 |
| `2` | 정상적인 확인/이미지 대기. `status`를 보고 필요한 작업 후 재개 |
| `130` | 사용자가 중단. 같은 작업 ID로 재개 가능 |

## 산출물과 검수

기본 산출물은 모두 `work/<작업 ID>/` 안에 있습니다.

- `out/final.mp4`: 1080×1920 영상
- `out/description.txt`: 제목·출처·음성/이미지 안내·크레딧
- `out/review.json`: 길이·컷·자막·사실 확인·음성/이미지 경고
- `out/upload_sound.txt`: 배경음악을 업로드 때 추가하는 설정을 쓴 경우의 안내
- `script_final/`, `voice/`, `assets/`, `timeline.json`, `state.json`: 재개와 수정용 자료

제작 완료는 영상이 렌더됐다는 뜻입니다. Codex가 실제 영상·음성 또는 확인 가능한 프레임을 검토하고 확인 범위를 구별해야 합니다. 과거 Cloudflare의 자동 영상 평가와 유료 효과음 청취는 실행하지 않습니다. 효과음은 기존 manifest·파일명·로컬 측정값을 사용합니다. YouTube 업로드와 Google 인증은 이 파이프라인에 없습니다.

Google Drive 검색·복사·이동, 완료 후 원본 삭제는 하지 않습니다. 수동 정리 전까지 중간 자료를 유지합니다.

## 테스트

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

기본 테스트는 외부 계정 추론 없이 가짜 Codex/TTS 실행 파일과 실제 이미지·WAV·FFmpeg를 사용합니다. 실제 모델·브라우저가 필요한 테스트는 준비되지 않으면 생략합니다. 브라우저가 준비된 맥에서는 `RECAP_TEST_BROWSER=1`로 로컬 HTML 캡처 테스트를 실행할 수 있습니다. 이 저장소의 테스트 성공만으로 사용자 맥의 Qwen/MPS·Codex 로그인·native imagegen 실연결이 검증되지는 않습니다.

제작 규격: [설계](docs/02_program_design.md), [효과·자막 설정](docs/fx_config.md). 기존 MIT 라이선스와 폰트·효과음 출처 문서는 유지합니다.
