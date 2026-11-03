# 02. 제작 프로그램 설계

macOS/Codex 실행과 기존 로컬 모델 연결은 [README](../README.md) 및 [맥 인수인계](macos_codex.md)를 따른다.

- 목표: **주제 하나 → 35초 안팎의 9:16 이미지 슬라이드형 쇼츠 1편**을 끝까지 자동으로 만들고, 사람이 검수한다.
- 입력: 디시인사이드 글(실시간 베스트·갤러리) 또는 블라인드 인기글 하나. 글 주소를 넣거나 인기글 목록에서 고른다.
- 출력: `out/final.mp4`(1080×1920), `out/description.txt`(설명 문구), `out/review.json`(검수표).
- 근거: 규격표 값은 공개된 쇼츠 32편을 재서 얻은 중앙값이다.

## 1. 원칙

1. **형식은 숫자로 맞춘다.** 아래 규격표의 값은 32편 측정의 중앙값에서 왔다. 결과물을 같은 자로 다시 재서 비교한다.
2. **사실은 근거가 있는 것만.** 대본의 사실 문장마다 근거 구절(커뮤니티 글 본문)을 붙이고, 프로그램이 그 구절이 원문에 실제로 있는지 확인한다. 커뮤니티 글에서 온 내용은 "커뮤니티에서 화제"처럼 **전해 들은 말로 표시**한다.
3. **실존 인물**: 사람을 닮은 AI 이미지 금지, 확인 안 된 논란·사생활은 주제에서 제외, 닉네임·IP는 저장하지 않고 화면에서 가린다.
4. **모든 자료에 출처 기록**(`assets/manifest.jsonl`): 글 캡처, 라이선스 사진, AI 생성 이미지(생성 모델·프롬프트), 뉴스 카드.
5. **뉴스 사이트는 열지 않는다.** 뉴스 카드는 직접 적은 목록(`--news-json`, `recap_shorts/collect/__main__.py`)으로만 만든다. 뉴스 검색 API 는 쓰지 않는다.
6. 업로드는 하지 않는다. 결과물과 검수표까지.

## 2. 규격표 (기본값)

| 항목 | 값 |
|---|---|
| 화면 | 1080×1920, 30fps, 검은 바탕 |
| 고정 제목 띠 | 화면 높이 0~24.2%. 두 줄, 1줄 흰색·2줄 노란색(#FFD400 근처), 굵은 고딕, 영상 내내 고정 |
| 이미지 상자 | 24.2~66.4% (1080×810, 4:3), 폭 전체 |
| 자막 띠 | 66.4~75.8% (1275~1455px), 기본 2줄, 넘치면 3줄까지 80%(1535px). 흰색 + 강조 낱말 1개 노랑/주황 |
| 아래 빈 영역 | 75.8~100% 비움 (쇼츠 앱 UI 자리) |
| 훅 흰 띠 | 0~첫 컷(2.4~2.9초, 기본 2.67초), 이미지 상자 위쪽에 흰 띠 한 줄(검은 글씨). 문구 = 첫 문장(18~24자). 이 동안 자막 띠는 비움 |
| 첫 이미지 | 훅 동안 천천히 확대(1.00→1.06~1.08) |
| 이후 이미지 | 1장당 1.2~1.6초(중앙 1.33초, 0.61회/초), 기본 정지, 3장 중 1장 정도 짧은 등장 효과(0.2초 흐림+이동) |
| 자막 카드 | 약 8음절(6~10), 약 1.2초. **카드 교체를 컷과 맞춘다**(측정에서 83%가 같이 넘어감) |
| 길이 | 30~38초 (대본 190~220음절) |
| 내레이션 | 첫 말 0.15초 이하, 끝까지 이어짐(말 비중 95% 이상, 문장 사이 쉼 0.3초 이하), 6.3~6.7음절/초 |
| 말투 | 반말 구어체, 전해 들은 말 연결("~했다는데", "~하더니"), 마지막 한 줄은 반응(감탄·평가·물음), 구독 요청 없음 |
| 소리 | 전체 −15 LUFS 근처(−16~−14). 배경음악은 목소리보다 약 22dB 아래(−20~−24)를 목표로 하고, 기본은 영상에 굽지 않고 올릴 때 앱의 '사운드 추가'로 넣는다(`bgm_mode`, docs/fx_config.md). 컷 효과음은 `config/fx.json` 에서 켜고 끈다(기본 켜짐) |

> 위 값은 32편의 측정 중앙값을 따른다. 띠 경계·색 상수는 `recap_shorts/render/layout.py` 에 있다.

## 3. 파이프라인과 모듈

```
source(글·캡처) → read(글자 읽기) → script(LLM) → voice(TTS) → assets(캡처 카드·AI 그림) → scenes(배치) → render(timeline→mp4) → review
```

| 단계 | 모듈 | 입력 → 출력 |
|---|---|---|
| `source` 글 가져오기 | `recap_shorts/collect/dcinside.py`·`blind.py`·`dcbest.py` | 글 주소 → 글 본문·첨부 그림, 글 머리·댓글 캡처(닉네임·IP·회사명 가림) |
| `read` 자료 읽기 | `recap_shorts/pipeline.py`(step_read), `llm/codex.py` | 첨부 그림·댓글 캡처 → `topic.json`(옮겨 적은 글자·종류·얼굴/아이디 표시) |
| `script` 대본 | `recap_shorts/script/` | `topic.json` → `script_final/script.json`(제목 2줄, 훅, 본문 문장[근거 포함], 끝 한 줄, 장면 설명) + `script_check.json`(사실 가드) |
| `voice` 목소리 | `recap_shorts/voice/` | `script.json` → `voice/voice.wav`, `voice/timings.json`(문장별 시각·받아쓰기 확인) |
| `assets` 자료 화면 | `recap_shorts/images/` | 장면 설명 → 글 캡처 카드(가림)·대상 사진·AI 그림 + `assets/assets.json`, `assets/manifest.jsonl` |
| `scenes` 장면 배치 | `recap_shorts/images/arrange.py`, `pipeline.build_plan` | 문장×그림 후보 → `shot_plan.json`(문장마다 보여 줄 그림) |
| `render` 영상 만들기 | `recap_shorts/timeline.py`, `recap_shorts/render/` | `timeline.json` → `out/final.mp4` |
| `review` 검수 | `recap_shorts/pipeline.py`(step_review), Codex의 직접 검수 | 영상·규격 → `out/review.json`, `out/description.txt` |

### 3.1 데이터 형식 (모듈 사이 계약)

`script.json`
```json
{
  "title_lines": ["1줄(흰색, 14자 이내)", "2줄(노란색, 14자 이내)"],
  "hook": "훅 흰 띠 문구 = 첫 내레이션 문장 (18~24자)",
  "sentences": [
    {"text": "본문 문장", "evidence": "근거 구절 그대로 또는 null", "source_id": "post|null",
     "emphasis": "자막에서 색을 줄 낱말 하나(선택)", "image_hint": "이 문장 동안 보여 줄 장면 설명",
     "take": "우리 해석 문장이면 true (선택, 옛 대본엔 없음)"}
  ],
  "closing": "끝 한 줄(반응)",
  "flags": ["확인 필요한 문장 번호 등"]
}
```
- `evidence`가 null인 문장은 사실 주장이 아니어야 한다(반응·연결 문장만). 사실 주장인데 근거가 없으면 생성 단계에서 버린다.
- `take: true`는 글 내용을 다시 전하는 말이 아니라 우리 쪽 정리·맥락·비교·관점인 본문 문장이다(대본 규칙 F9). 가드(`script/guard.py check_take`)가 대본에 해석이 하나도 없으면 `script_check.json` 의 `flags` 에 경고를 남기고(막지 않음), 검수 화면의 대본 경고로 올라간다.
- `image_hint`는 "AI 장면: …" 과 " / 캡처: …"(글·댓글의 어느 줄)를 함께 적는다. 장면 배치가 앞쪽부터 캡처를 채우고 AI 그림은 남는 자리에만 넣기 때문이다.

`timeline.json` (렌더 입력)
```json
{
  "fps": 30, "size": [1080, 1920], "duration": 35.2,
  "title_lines": ["...", "..."],
  "hook": {"text": "...", "start": 0.0, "end": 2.6},
  "shots": [{"file": "assets/...png", "start": 0.0, "end": 2.6, "motion": "zoom_in|static|enter",
             "focus": [0.5, 0.5], "source_id": "asset_id"}],
  "captions": [{"text": "...", "start": 2.6, "end": 3.9, "emphasis": "낱말", "breaks": [[1, 3.2]]}],
  "audio": {"voice": "voice/voice.wav", "bgm": null, "bgm_db": -22.0, "sfx_on_cuts": false},
  "options": {"snap_captions_to_cuts": true}
}
```
- `breaks`는 문장 안에서 자막 카드를 끊을 자리(원문 어절 경계 번호, 시각)다. 컷과 자막이 같은 시각에 바뀌게 한다.
- `audio.bgm` 은 "요청"일 뿐이고, 기본 설정(`bgm_mode: "upload_note"`)에서는 렌더러가 영상에 섞지 않는다. 대신 `out/upload_sound.txt` 에 올릴 때 넣는 법을 적는다.
- 짤·효과음을 켜면 `fx` 단계가 `overlays`, `audio.sfx`, 전체화면 짤 샷(`kind: "meme"`), 기록용 `fx` 를 덧붙인다(docs/fx_config.md).

## 4. 모델·도구

| 일 | 쓰는 것 | 메모 |
|---|---|---|
| 대본·비전 | 기존 ChatGPT 로그인으로 `codex exec` | 현재 Codex 모델 설정을 사용. API 키와 별도 OAuth 토큰을 읽지 않음 |
| 목소리 | 기존 로컬 Qwen/MLX 명령(기본) 또는 기존 Supertonic | 모델·가상환경·참조 음성을 교체하지 않고 실행 명령만 연결 |
| 낱말 시각 | 문장 단위 TTS 길이 + 문장 안 음절 수 비례 | 받아쓰기(SenseVoice)가 있으면 글자 시각으로 보정하고, 문장 안 그림 컷을 어절 경계에 놓는 데도 쓴다(`recap_shorts/wordcuts.py`) |
| 이미지 생성 | 현재 Codex 세션의 내장 imagegen | `assets/imagegen_tasks.json` → 실제 PNG 저장 → `resume`; 파일이 없으면 `awaiting_images`에서 대기 |
| 라이선스 사진 | Wikimedia Commons, Openverse | 허용 라이선스만 남기고 출처 표기 문자열을 저장 |
| 렌더 | Pillow 로 프레임 합성 → ffmpeg(libx264, yuv420p) | 글꼴 `assets/fonts/Pretendard-ExtraBold.otf`·`Pretendard-Black.otf`, 없으면 맑은 고딕/Noto 로 물러남 |
| 배경음악 | 기본은 영상에 굽지 않음(`bgm_mode: "upload_note"`) | `timeline.audio.bgm` 을 요청하면 `out/upload_sound.txt` 에 곡·시작 위치·크기 힌트를 적는다(docs/fx_config.md) |

## 5. 하지 않는 것 (MVP)

업로드, 예약, 썸네일, 여러 변형 동시 생성, 추천 순서. 하나를 끝까지 제대로 만든 뒤 늘린다.
