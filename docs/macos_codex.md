# 맥 Codex 인수인계

## 이 변경의 범위

이 포크는 Windows 앱을 설치하는 프로젝트에서, **맥의 Codex가 기존 로컬 모델을 조합해 제작하는 프로젝트**로 바뀌었습니다. 기존 글·이미지 수집, 근거 확인, 음성 타이밍, 자막·효과·렌더 코드는 유지합니다. 외부 유료 AI 호출과 별도 OAuth, Google Drive 및 GUI를 제거합니다.

사용자 맥의 실제 모델 이름·설치 위치는 원격 저장소에서 확인할 수 없으므로 임의 경로를 기본값으로 넣지 않았습니다. 첫 실행은 현재 맥의 정상 동작 명령을 확인하고 연결하는 단계입니다.

## 맥 Codex에게 줄 시작 지시

```text
이 저장소의 AGENTS.md와 README.md를 읽고 맥 환경 연결을 진행해.
현재 이 맥미니에서 정상 작동하는 Qwen TTS 또는 MLX TTS의 기존 실행 명령,
가상환경, 모델 경로, 화자/참조 음성 설정을 확인해서 그대로 사용해.
새 모델을 설치하거나 기존 환경을 업그레이드·교체하지 마.
config/runtime.local.json에 실제로 확인한 명령과 경로를 연결하고 doctor를 실행해.
Codex는 기존 ChatGPT 로그인, 이미지는 이 세션의 내장 imagegen을 사용해.
기존 TTS 설정을 하나로 특정할 수 없으면 후보와 차이를 보여주고 물어봐.
확인하지 않은 실제 추론·이미지 생성 성공을 성공했다고 보고하지 마.
```

첫 지시는 환경 연결까지만 수행합니다. 실제 영상 제작에는 사용자가 고른 게시글 URL이나 근거 원문을 추가로 제공합니다.

## 1. 기존 TTS 연결 방식

### 기존 명령 유지 — 권장

`config/runtime.local.json`의 `qwen_command`는 정확한 argv 배열입니다. 쉘에서 `eval`하지 않습니다.

```json
{
  "qwen_command": [
    "/기존/가상환경/bin/python",
    "/기존/실행스크립트.py",
    "--text-file", "{text_file}",
    "--output", "{output}",
    "--model", "/기존/모델폴더"
  ]
}
```

위 옵션은 **예시**입니다. 원래 도구가 지원하는 옵션을 먼저 확인하세요. 기존 화자·참조 음성·모델·샘플레이트 설정을 보존합니다. MLX 실행기가 output prefix나 디렉터리만 받는다면, 원래 명령을 호출한 뒤 실제 결과 WAV를 `{output}`으로 옮기는 작은 로컬 wrapper를 사용합니다. 모델을 다시 구현할 필요는 없습니다.

| 치환 | 내용 |
|---|---|
| `{text}` | 합성할 전체 내레이션 문자열 |
| `{text_file}` | UTF-8 내레이션 텍스트 파일 |
| `{output}` | 반드시 생성해야 할 WAV 파일의 절대 경로 |
| `{request_json}` | 아래 요청 JSON 파일의 절대 경로 |
| `{instruct}` | 내레이션 스타일 지시문; 기존 도구가 지원할 때만 전달 |
| `{seed}` | 재현 가능한 합성에 쓸 정수 |
| `{language}` | `Korean` |

명령은 정상 종료해야 하며, 새 WAV가 디코딩 가능하고 샘플 수·샘플레이트가 유효해야 합니다. 과거 WAV가 남아 있다는 이유로 성공 처리하지 않습니다. 모델 설치·다운로드·업데이트는 실행하지 않습니다.

### 기존 Python + 요청 JSON 어댑터

기존 Python 가상환경을 지정하는 경우 symlink 경로를 그대로 실행하여 해당 가상환경을 보존합니다.

```json
{
  "qwen_python": "/기존/가상환경/bin/python",
  "qwen_script": "/기존/request_json을_받는_스크립트.py"
}
```

스크립트는 인수 하나로 요청 JSON 경로를 받습니다. 현재 계약은 `recap_shorts/voice/qwen.py`의 `run_cli()`가 기록하는 내용을 기준으로 합니다. 주 필드는 `text`, `out`, `instruct`, `seed`, `language`입니다. 결과 파일은 요청의 `out` 경로에 저장합니다. stdout에 CUDA 메타데이터를 출력할 필요는 없습니다.

저장소의 선택적 `tools/qwen_tts/synth_cli.py`를 사용하려면 **기존 PyTorch Qwen VoiceDesign 환경**과 `qwen_model`이 있어야 합니다. `qwen_device`는 `auto`, `cpu`, `mps`, `cuda`를 지원하지만 실제 모델의 MPS 지원은 해당 맥에서 확인해야 합니다. 이 어댑터는 기존 캐시/모델만 읽으며 네트워크 다운로드를 막습니다. 기존 MLX/Base/참조 음성 방식은 원래 명령을 연결하세요.

## 2. 설정 우선순위와 보존

`python -m recap_shorts`는 시작할 때 `config/runtime.local.json`을 읽습니다. `RECAP_RUNTIME_FILE`로 다른 파일을 지정할 수 있습니다. 명시적으로 설정한 환경 변수가 로컬 JSON보다 우선합니다.

| JSON 키 | 환경 변수 |
|---|---|
| `qwen_command` | `RECAP_QWEN_COMMAND_JSON` |
| `qwen_python` | `RECAP_QWEN_PYTHON` |
| `qwen_script` | `RECAP_QWEN_SCRIPT` |
| `qwen_model` | `RECAP_QWEN_MODEL` |
| `qwen_device` | `RECAP_QWEN_DEVICE` |
| `qwen_timeout` | `RECAP_QWEN_TIMEOUT` |
| `sensevoice_dir` | `RECAP_SENSEVOICE_DIR` |
| `supertonic_dir` | `RECAP_SUPERTONIC_DIR` |
| `codex_path` | `RECAP_CODEX_PATH` |
| `browser_path` | `RECAP_BROWSER_PATH` |
| `browser_channel` | `RECAP_BROWSER_CHANNEL` |

`HOME`, `PATH`, `CODEX_HOME`, 인증 저장소, TLS/proxy 설정을 바꾸지 않습니다. 런타임 파일은 API 키나 OAuth 토큰을 받지 않습니다. 개인 경로를 포함하는 `*.local.json`은 GitHub에 올리지 않습니다.

`config/settings.local.json`은 `image_engine`, 선택적 Codex `models.script`, 로컬 `voice.engine`, 효과음·짤 폴더에 사용합니다. `models.script`가 비어 있으면 Codex의 기존 설정을 계승합니다. 이전 Gemini 설정은 유료 경로를 활성화하지 않습니다.

## 3. 제작과 이미지 작업 계약

파이프라인은 `source → read → script → voice → assets → scenes → render → review` 순서입니다. `--topic-file`은 수집·자료 읽기를 완료한 텍스트 입력으로 취급합니다.

`assets`가 생성 이미지를 필요로 하면 `imagegen_tasks.json`을 저장하고 `awaiting_images`로 멈춥니다. 이 상태는 사람이 완료한 단계를 확인하는 `waiting`과 다릅니다.

```bash
.venv/bin/python -m recap_shorts image-tasks 작업_ID
```

Codex는 manifest를 읽고 실제 native imagegen 도구를 호출합니다. `output`은 manifest 폴더 기준입니다. 권장 가로 4:3 PNG이며, Python은 최소 256px·파일 디코딩·방향을 검사합니다. 프롬프트가 바뀌면 이전 파일을 새 이미지로 인정하지 않습니다. 파일을 저장한 뒤:

```bash
.venv/bin/python -m recap_shorts resume 작업_ID
```

`--approve`도 이미지 파일 검증을 건너뛰지 못합니다. source/script/voice 등 완료한 단계는 재사용합니다. 한 work 폴더에는 쓰기 명령 하나만 실행할 수 있으며, 프로세스가 종료되면 잠금은 해제됩니다.

## 4. 중단 원인별 처리

| 상태/오류 | 다음 작업 |
|---|---|
| Codex CLI 없음 | 이미 설치한 CLI 경로 확인 후 `codex_path` 연결 |
| ChatGPT 로그인 없음/만료 | `codex login status` 확인, 필요한 경우 `codex login` |
| Codex 사용 한도 | 같은 작업을 유지하고 한도 회복 뒤 재개 |
| 기존 TTS 명령 불명 | 기존 환경을 확인하거나 사용자에게 실행 명령 문의 |
| Qwen/ASR 모델 없음 | 기존 모델 경로 확인. 새 모델 자동 설치 금지 |
| ASR을 사용하지 않기로 결정 | 새 작업에 `--no-asr`; 음성·장면·FX의 ASR을 모두 비활성화 |
| `awaiting_images` | native imagegen 실제 생성 → 지정 PNG 저장 → 재개 |
| native imagegen 없음 | 현재 요청을 pending으로 유지하고 기능 부재 보고 |
| `waiting` | 완료 단계 산출물 확인 후 `resume ID --approve` |
| `error`/프로세스 중단 | 로그 확인·원인 수정 후 같은 ID로 재개 |

## 5. 검증 범위

기본 테스트는 실제 계정·모델 추론 없이 인증 라우팅, 명령 인수, 파일 계약, 대기·재개, 기존 TTS 환경 보존과 로컬 렌더를 검증합니다. 실제 맥에서 남는 확인은 기존 Qwen/MLX 짧은 합성, 현재 Codex 로그인으로 대본 한 번, 해당 세션의 내장 imagegen 한 장, 최종 영상·자막·음성 점검입니다.

`doctor.ready`는 사전 점검이며 모델 로딩·캐시 ID 실재 여부·계정 사용 한도·native imagegen 가용성을 모두 증명하지 않습니다. 완료 보고에는 실제로 실행한 항목과 확인하지 못한 항목을 구별하세요.
