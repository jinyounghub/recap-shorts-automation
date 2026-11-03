# 짤(밈)·효과음 설정 (config/fx.json)

짤·효과음·인트로 카드는 기본 **켜짐**이다(맨 위 `enabled`, `memes.enabled`, `sfx.enabled`, `intro.enabled` 모두 true). 설정 파일은 `config/fx.json` 이고, 작업(job)마다 `options.fx` 로 덮어쓸 수 있다.

## 켜고 끄는 법

| 방법 | 효과 |
|---|---|
| `config/fx.json` 의 `enabled`, `memes.enabled`, `sfx.enabled` | 장면 배치(`pipeline.write_timeline`)가 타임라인을 쓸 때 자동 적용 |
| 작업 옵션 `fx: true` / `fx: false` | 짤·효과음 둘 다 켬 / 끔 |
| 작업 옵션 `fx: {"memes": {"enabled": false, "library": "..."}, ...}` | 설정을 부분 덮어쓰기(켜짐으로 침). `enabled` 를 dict 로 주면 그 안의 `enabled` 가 이긴다 |
| `python -m recap_shorts.fx <job_dir> [--memes/--no-memes] [--sfx/--no-sfx] [--no-llm] [--config 파일] [--render] [--out 이름.mp4]` | 기존 작업의 `timeline.json` 은 그대로 두고 `timeline_fx.json` 을 쓴다. `--render` 면 `out/<이름>.mp4`(기본 `timeline_fx.mp4`)까지 만든다 |

인트로 카드는 짤·효과음과 따로 간다 — `fx` 를 꺼도, fx 단계가 실패해도 붙는다(`fx/apply.with_intro`). 인트로를 끄는 곳은 `intro.enabled` 하나다.

짤 라이브러리가 없거나 비어 있으면 짤은 건너뛰고 보고서(`fx.notes`)에만 남는다(오류 아님). 효과음도 같다. fx 단계에서 예외가 나도 파이프라인은 멈추지 않고 `timeline["fx"]["error"]` 만 남긴다.

## 흐름

1. **분위기 라벨(beats)** — `recap_shorts/fx/beats.py`. 문장마다 라벨 하나:
   `shock funny sad angry smug awkward reveal question conclusion neutral`.
   영상 한 편에 Codex 텍스트 호출 1번(`beats.model`, 기본 `null` (현재 Codex 모델)). 길이가 다르거나 호출 실패·로그인 없음이면 규칙(물음표→question, `ㄷㄷ/실화/충격…`→shock, 마지막 문장→conclusion, 그 외 neutral)으로 대신하고, 모르는 라벨 한 칸만 틀리면 그 칸만 규칙 값으로 채운다. 결과는 작업 폴더 `fx/beats.json` 에 저장 — 문장이 같으면 다시 부르지 않는다(실패한 기록도 다시 부르지 않음. 다시 하려면 파일을 지운다).
2. **짤 고르기** — `fx/memes.py`(+ `pick_by: "fit"` 이면 `fx/meme_fit.py`).
3. **효과음 놓기** — `fx/sfx.py`.
4. 타임라인에 `overlays`, `audio.sfx`, (전체화면 짤이면) `kind:"meme"` 샷, 기록용 `fx` 를 덧붙인다 — `fx/apply.py`.

## 설정 항목

### beats
| 키 | 기본값 | 뜻 |
|---|---|---|
| `use_llm` | `true` | false 면 Codex 를 부르지 않고 규칙만 |
| `model` | `null` (현재 Codex 모델) | 텍스트 모델 |
| `cache` | `fx/beats.json` | 작업 폴더 안 저장 위치 |

### memes
| 키 | 기본값 | 뜻 |
|---|---|---|
| `enabled` | `true` | 짤 켜기 |
| `library` | `assets_local/memes_r` | 기본 짤 폴더(저장소 루트 기준 또는 절대경로). `manifest.json` 필요. **`config/settings.local.json`의 `memes.dirs`에 폴더를 지정하면 그 폴더들이 먼저**(`config/settings.local.json` 의 `memes.dirs`, 분석한 꾸러미 또는 manifest 가 있는 폴더, manifest 없는 폴더는 파일 이름을 뜻으로) |
| `position` | `"end"` | `"end"` = 그 문장이 끝나는 쪽(다음 문장 시작, 마지막 문장이면 영상 끝)에 반응으로. `"start"` = 문장 시작 |
| `min_before_s` | `0.6` | `position: "end"` 에서 문장 시작 뒤 이만큼보다 앞으로는 안 간다 |
| `max_per_video` | `2` | 한 편에 넣을 최대 개수(길이표가 없을 때) |
| `max_by_length` | `[[40, 2], [50, 3]]` | 영상 길이(초) 상한별 최대 개수 |
| `max_cap` | `3` | 길이표를 넘긴 길이의 상한 |
| `max_per_family` | `{"pepe": 1}` | 계열별 상한(`"*"` 키로 나머지 계열) — 실효 최대 개수는 길이 상한과 이 값 중 작은 쪽 |
| `family_alias` | `{"apu": "pepe"}` | 계열 이름 묶기 |
| `min_gap_s` | `3.0` | 짤 시작 사이 최소 간격(초) |
| `allowed_beats` / `beat_priority` | question·angry·reveal·smug·awkward·shock·funny·sad·neutral | `pick_by: "beats"` 에서 붙일 수 있는 분위기 / 후보가 많을 때 먼저 뽑을 순서 |
| `on_hook` | `false` | 훅(첫 문장)에도 붙일지 |
| `first_after_s` | `3.0` | 첫 짤은 이 시각 이후 |
| `end_with_meme` | `false` | 마지막 본문 문장에 `conclusion` 짤 1개(라이브러리에 없으면 note 만 남기고 건너뜀) |
| `prefer_transparent` | `true` | 투명 배경 짤을 스티커로 먼저 |
| `avoid_same_family_twice_in_a_row` | `true` | 가까운 시각에 같은 계열 연속 피하기 |
| `pick_by` | `"fit"` | `"fit"` = 문장 전체와 짤의 뜻(`manifest` 의 `meaning`)을 Codex 가 함께 보고 반응이 자연스러운 문장에만 고른다(영상당 1회, 결과 `fx/meme_fit.json`. 어울리는 곳이 없거나 호출이 실패하면 짤 없이 감). `"beats"` = 분위기 라벨로 고르기 |
| `full_shot_ratio` | `1.0` | 뽑힌 짤 중 전체화면 샷으로 쓸 비율(0=전부 스티커, 0.25=네 번째마다) |
| `full_shot_len_s` / `full_shot_min_s` / `full_shot_max_s` | `1.2` / `0.6` / `2.0` | 전체화면 짤 길이(문장 끝을 넘지 않음) |
| `full_shot_bg` | `[255, 255, 255]` | 전체화면 짤 배경색 RGB |
| `full_shot_fit` / `wide_aspect` / `full_shot_focus_y` / `full_shot_margin` | `"cover"` / `1.25` / `0.35` / `0` | 그림 맞춤(`auto`=가로로 넓은 짤만 cover, `contain`, `cover`), cover 로 자를 때 세로 초점, 상자 여백(px) |
| `full_shot_anim` | `"none"` | 전체화면 짤 등장 효과(`none`·`pop`) |
| `lead_s` | `0.0` | 문장 시작 대비 짤 시작 시각 이동(초) |
| `sticker.size_frac` | `0.38` | 이미지 상자 가로 대비 크기(38%) |
| `sticker.anchors` | `["box_br", "box_bl"]` | 위치를 돌려 가며 씀(`box_br box_bl box_tr box_tl box_center`) |
| `sticker.anim`, `sticker.anim_by_beat` | `"none"`, `{}` | `pop`(등장 확대+작은 오버슈트) / `shake`(등장 뒤 흔들림) / `slide_up`(아래서 올라옴) / `none`(효과 없이), 분위기별 지정 |
| `sticker.max_len_s` | `1.8` | 스티커가 떠 있는 최대 시간(문장 끝 전에 끝남) |
| `sticker.pop_s`, `overshoot` | `0.18`, `1.12` | 등장 시간, 오버슈트 배율(12% 넘게 커졌다 돌아옴) |
| `sticker.fade_s` | `0.12` | 사라질 때 투명해지는 시간 |
| `sticker.shake_px` | `10` | 흔들림 폭 |
| `sticker.outline_px`, `margin_px` | `6`, `24` | 흰 테두리(+그림자) 두께, 상자 가장자리 여백 |

규칙: 한 편에서 같은 짤 반복 없음, 훅 제외(`on_hook`), 시작 간격 ≥ `min_gap_s`, 같은 입력이면 같은 결과(작업 폴더 이름이 씨앗). manifest 항목의 `has_text: true`(예: 글자가 크게 박힌 짤)는 고르는 순서에서 뒤로 미룬다 — 같은 박자에 다른 후보가 있으면 그쪽이 먼저 뽑히고, 그것뿐이면 뽑힌다(`fx/memes.py` 의 정렬 키).

#### 짤 manifest (`assets_local/memes_r/manifest.json`)
```json
{"version":1,"items":[{"id":"pepe_sad_01","file":"pepe_sad_01.png","name":"슬픈 페페","family":"pepe",
  "beats":["sad"],"meaning":"슬프거나 안타까운 반응. 득의양양한 장면에는 안 맞음",
  "transparent":true,"size":[w,h],"source_url":"https://example.invalid/meme","source_site":"example",
  "license":"unknown","retrieved_at":"ISO"}]}
```
`meaning` 은 `pick_by: "fit"` 이 문장과 짤을 짝지을 때 쓰는 설명이다(없으면 `name`). 파일이 실제로 없는 항목은 무시한다.

### sfx
| 키 | 기본값 | 뜻 |
|---|---|---|
| `enabled` | `true` | 효과음 켜기 |
| `libraries` | `["assets/sfx_packs/user_meme", "assets/sfx_packs/chime"]` | 기본 효과음 폴더들(저장소에 같이 든 꾸러미, `assets/sfx_packs/README.md`). manifest 에 `source`·`license` 를 적는다. **로컬 설정의 효과음 폴더에서 폴더를 고르면 그 폴더가 먼저**(`config/settings.local.json` 의 `sfx.dirs`, 없는 종류는 `sfx.fill_missing` 이 켜져 있으면 여기서 채움). 비어 있으면 `fallback_libraries` → 저장소 합성음(`assets/sfx`) 순서로 찾고, 그것도 없으면 건너뛴다 |
| `fallback_libraries` | `[]` | 설정한 꾸러미가 없는 PC 에서 대신 쓸 폴더 |
| `min_gap_s` | `0.5` | 효과음 시작 사이 최소 간격. 겹치면 우선순위(끝 > 짤 > 훅 > 분위기 > 컷)로 남긴다 |
| `max_per_video` | `24` | 최대 개수(우선순위 낮은 것부터 버림). 인트로·끝 소리는 이 상한 밖 |
| `max_per_10s` | `2.3` | 본문 10초당 최대 개수(훅이 끝난 뒤 구간 기준) |
| `max_same_file` | `4` | 같은 파일을 한 영상에서 몇 번까지 |
| `avoid_last_n` | `1` | 직전 n개와 같은 파일 피하기 |
| `choose` | `"rotate"` | 같은 종류 안에서 고르는 방식(`rotate`·`fixed`) |
| `place_on` | `"none"` | `"speech_gap"` = 효과음 시작을 계획 시각 ±`gap_window_s` 안에서 목소리 10ms RMS 가 가장 낮은 곳(숨 고르기)으로 옮긴다(훅 자리만 그대로). 목소리를 못 읽으면 note |
| `gap_window_s` | `0.25` | 위 이동 폭(초) |
| `cuts.mode` | `"all"` | `none` / `every_k`(k 번째 컷마다, `cuts.k`) / `sentence_start`(새 문장이 시작하는 컷만) / `all` |
| `cuts.skip_ratio` | `0.56` | 컷의 일부를 결정적 해시로 건너뜀 |
| `cuts.types` | sparkle .26 · ding .22 · pop .20 · boom .11 | 종류별 가중치(황금비 수열로 컷 번호마다 결정적 순환) |
| `cuts.type` | `"pop"` | `types` 가 없을 때 쓰는 종류 |
| `cuts.db`, `offset_s`, `skip_first` | `-17`, `0.03`, `false` | 컷 효과음 크기·시차, 훅 뒤 첫 컷 제외 |
| `meme_pop.by_beat` | shock→boom, reveal→boom, question→huh, angry→scream, awkward→awkward, sad→sad, funny→laugh, smug→sus, conclusion→punchline | 짤이 뜰 때 분위기별 효과음 종류 |
| `meme_pop.default_type`, `rest_from_cuts` | `"pop"`, `true` | 위 표에 없는 분위기는 컷 순환을 따름 |
| `meme_pop.db`, `offset_s` | `-20`, `0.23` | 짤 효과음 크기·시차 |
| `hook.enabled` | `true` | 인트로·훅 효과음 묶음 |
| `hook.open` | `{type:"hit", t:0.06, db:-3}` | 영상 시작 '탁' |
| `hook.pop` | `{enabled:false, type:"whoosh_short", offset_s:0.06, db:-8}` | 인트로 카드가 튀는 순간(인트로가 없으면 건너뜀) |
| `hook.first_cut` | `{type:"pop", offset_s:0.02, db:-15}` | 훅이 끝나는 첫 컷 '톡' |
| `ending.enabled` | `false` | 끝 효과음. `at_end_s`(1.1)면 영상 끝 1.1초 전, `len_s`(0.4) 길이로 자르고 끝을 줄임, `db` −18, `types` sparkle .3·ding .2·pop .1 |
| `beat_events.enabled` | `false` | 분위기 문장 시작에 놓는 효과음(`by_beat` reveal→ding, shock→boom, `db` −18) |
| `types_db` | boom −15, pop −15, sparkle −15, ding −23, huh −21, scream −22 … | 종류별 크기 강제 값 |
| `type_fallback` | sparkle→ding, ding→sparkle, slidewhistle→boing, cheer→laugh … | 라이브러리에 그 종류가 없을 때 대신 쓸 종류. 기본 밈 꾸러미에는 딩(상표 소리라 뺌)이 없어 ding → sparkle(차임) |
| `ban_sources` | `["emotional-damage-auughhh"]` | 쓰지 않을 소리(원본 파일 이름 앞부분으로 뺀다) |
| `avoid_rights_risk` | `null` | `null`(고르기 그대로) / `"high"` / `"medium"`(그 등급 이상을 후보에서 뺌) — 아래 '효과음 저작권 점검' |

효과음 종류: 기본 합성 꾸러미는 `whoosh swoosh pop ding boom click typing riser buzz boing shutter coin slidewhistle`. 같은 종류가 여러 파일이면 번갈아 쓴다.

**db 의 기준**: "목소리 피크 목표(−1.5 dBTP) 대비". 효과음 파일은 피크 정규화돼 있고 믹스할 때 파일 피크를 읽어 `−1.5 + db` 가 되게 맞춘다. 예: `db −10` → 피크 −11.5 dBFS. 목소리는 −15 LUFS 로 맞추며, 효과음이 있을 때는 합친 뒤 0.95 한계 리미터(자동 레벨 보정 끔)로 막아 클리핑이 없다.

효과음 만들기: `python scripts/make_sfx.py` → `assets/sfx/*.wav` + `manifest.json`(48kHz 모노, 시드 고정).

#### 효과음 폴더 (manifest 가 있거나 없거나)

`manifest.json` = `{"version": 1, "items": [{"id", "file", "type", "duration", "roles"?, "source_file"?, "license"?, "credit"?}]}`. 목록에 있고 파일이 실제로 있는 항목만 쓴다. `credit` 이 `필수:` 로 시작하면 그 뒤 문구를 영상 설명에 넣는다(쓴 소리가 든 폴더의 manifest 에서 찾음).

manifest 가 없는 폴더(설정 화면에서 고른 내 폴더)는 바로 아래와 한 단계 하위 폴더의 소리 파일(wav·mp3·ogg·flac·m4a, 폴더당 3000개까지)을 읽고 종류를 이름에서 정한다: 하위 폴더 이름이 종류면 그것(`pop/아무이름.mp3`), 아니면 파일 이름에서 끝 번호를 뗀 것(`pop_01.wav`, `boom.wav`, `whoosh_short-2.mp3`) 또는 앞 낱말(`pop_cartoon.wav`). 종류는 `recap_shorts/fx/sfx.py` 의 `KNOWN_TYPES`. 모르는 이름은 빼고 설정 화면에 뺀 개수를 보여 준다. 저작권 점검(`config/sfx_rights.json`)은 `pack` 이름(`user_meme`) 폴더만 본다.

설정 화면에서 `manifest.json` 이 없는 폴더를 넣으면 분석(`analyze`, 아래)을 거쳐 `assets_local/analyzed/sfx/<이름>/` 에 꾸러미를 만들고 그 폴더를 `sfx.dirs` 에 넣는다.

### analyze (효과음·짤 폴더 분석)

폴더 분석은 Python 함수 `recap_shorts.fx.library`를 사용한다. 짤 이미지의 시각 분석은 Codex 비전을 사용하고, 효과음은 기존 manifest·파일명·로컬 음향 측정값으로 분류한다. Codex 텍스트/비전에 오디오를 보내 청취했다고 표시하지 않는다. GUI·REST 서버는 사용하지 않는다.

| 키 | 기본값 | 뜻 |
|---|---|---|
| `model` | `null` (현재 Codex 모델) | 짤 비전 분석용 Codex 모델. null이면 현재 사용자 설정을 계승하며 효과음 청취에는 사용하지 않음 |
| `sfx.max_files` | `800` | 폴더에서 볼 소리 파일 수(하위 폴더 두 단계까지) |
| `sfx.max_file_s` | `40` | 이보다 긴 파일은 효과음이 아니라고 보고 듣지 않는다 |
| `sfx.listen_s` | `6` | 소리 시작부터 들려줄 길이 |
| `sfx.min_fit` · `sfx.min_quality` | `2` · `3` | 종류 어울림(0~3)·품질(1~5) 하한 |
| `sfx.max_clip_ratio` | `0.02` | 찢어진 표본 비율이 넘으면 뺌 |
| `sfx.drop_owner_clear` | `true` | 게임·브랜드·방송·노래·유명인 목소리처럼 권리자가 분명한 소리 빼기 |
| `sfx.want` | `{"pop": 4, "*": 3}` | 종류마다 고를 수 |
| `sfx.max_active_s` | hit 0.6 · pop 1.0 · ding 2.0 · sparkle 2.5 · boom 3.0 · 나머지 3.5 | 소리 길이가 넘으면 그 종류 후보에서 뺌 |
| `sfx.cap_s` | hit 0.35 · pop 0.6 · … · 나머지 1.8 | 다듬을 때 자르는 길이(끝 0.06초 페이드) |
| `meme.max_files` · `meme.min_side_px` | `400` · `120` | 볼 그림 수, 짧은 변이 이보다 작으면 뺌 |
| `meme.dup_distance` | `12` | 16×16 차이 해시(256비트) 거리가 이 이하이고 가로세로 비율이 8% 안이면 같은 그림(큰 것 하나만). 줄여 다시 저장한 사본은 5 안팎, 같은 캐릭터의 다른 표정은 20 넘음 |
| `meme.min_quality` | `2` | 품질(1~5) 하한 |
| `meme.max_side_px` | `1600` | 이보다 큰 그림은 줄여 PNG 로 복사 |

### timing
| 키 | 기본값 | 뜻 |
|---|---|---|
| `picture_lead_s` | `0.2` | 그림·자막이 말보다 이만큼 먼저 바뀐다. 샷·자막은 그대로 두고 **목소리만 늦추며** 영상이 그만큼 길어진다(마지막 샷도 늘림). 효과음 `t` 는 영상 시계 그대로 |
| `cuts_at_word_gaps` | `true` | 문장 안 그림 컷을 어절 경계에 놓는다(아래 '문장 안 그림 컷'). false 면 문장 구간을 똑같이 나눈다 |
| `word_cuts.asr` | `"auto"` | 어절 경계 시각을 받아쓰기로 잡기(`auto`=모델 있으면, `cache`=저장된 것만, `off`) |
| `word_cuts.min_len_s` | `[0.7, 0.55, 0.4]` | 샷 최소 길이를 차례로 낮춰 본다 |
| `word_cuts.good_len_s` | `[0.8, 2.2]` | 되도록 지키려는 샷 길이 |
| `word_cuts.lead_s` | `0.0` | 컷이 어절 경계보다 이만큼 먼저(0 = 경계에서 바로) |
| `word_cuts.snap_meme_s` | `0.4` | 문장 끝 짤 샷의 시작도 그 안에서 어절 경계로 옮김(0 = 안 옮김) |

### bgm_mode
| 값 | 뜻 |
|---|---|
| `"upload_note"` (기본) | `render/audio.build_mix` 가 `bgm` 을 ffmpeg 입력·필터에 넣지 않는다. 렌더가 끝나면 `out/final.mp4` 옆에 **`out/upload_sound.txt`** 를 쓴다 — 요청한 곡(파일 이름, 있으면 제목), 곡에서 시작할 위치, `bgm_db` 로 본 크기 힌트, '사운드 추가'로 넣는 법. 배경음악 요청이 없으면 아무 파일도 쓰지 않는다(파이프라인은 렌더 전에 지난 안내 파일을 지운다). 작업 산출물 `upload_sound` 로도 올라간다 |
| `"bake"` | 예전 방식(영상에 섞음). `render(..., bgm_mode="bake")` 로 한 번만 바꿀 수도 있다 |

타임라인 선택 필드: `audio.bgm_title`(곡 제목), `audio.bgm_start_s`(곡에서 시작할 위치, 초).
`python -m recap_shorts.timeline <job> --bgm 곡.wav --bgm-title 이름 --bgm-start 42`.
렌더 결과(`render()` 반환)에 `bgm_mode`, `bgm_skipped`, `upload_sound`(쓴 파일 경로 또는 None)가 들어간다.

### intro
0초~`t_cut` 동안 그림 상자에 흰 카드만 보인다 — 첫 샷·훅 흰 띠·자막은 안 그림. `t_pop` 에 판이 줄고 글자가 커진 뒤 `t_cut`(훅 끝)에 하드 컷.

| 키 | 기본값 | 뜻 |
|---|---|---|
| `enabled`, `style` | `true`, `"card"` | 인트로 켜기(끄는 곳은 여기 하나) |
| `label_default` | `"디시인사이드"` | `label_fixed` 가 비었을 때 실제 출처를 못 찾으면 쓰는 머리줄 |
| `label_fixed` / `label_icon` | `"\| 유머 • 정보 게시판 >"` / `"🤣"` | 모든 영상에 같은 머리줄. 비우면 실제 출처(topic.json 의 '디시인사이드 · 실시간 베스트' 꼴). 아이콘은 컬러 이모지 글꼴(`render/intro.py` `EMOJI_FONTS`: 윈도우 `seguiemj.ttf` → 맥 → Noto)로 글자 높이의 34/30 배로 그리고, 글꼴이 없으면 글자만 |
| `panel_color`, `header_color`, `title_color` | `#FFFFFF`, `#6DB386`, `#010101` | 카드·머리줄·제목 색 |
| `ref_box_h`, `panel_h_start`, `panel_h_end` | `1028`, `1028`, `792` | 인트로 기준 상자 높이 대비 우리 상자 높이로 비례(×0.788). `t_pop` 에 판 높이가 start→end(가운데 기준) |
| `header_px`, `title_px`, `line_gap_px` | `30`, `64`, `118` | 머리줄·제목 글자 크기, 두 줄 간격 |
| `header_cy`, `title1_cy` | `-150`, `-38.5` | 세로 위치 |
| `zoom_pre_per_s`, `zoom_post_per_s` | `0.014`, `0.006` | 글자 묶음이 튀기 전·후에 커지는 속도 |
| `pop_s`, `pop_text_scale`, `ease` | `0.067`, `1.084`, `"in_out"` | 튀는 시간, 글자 확대 배율, 느리게-빠르게-느리게 |
| `pop_lead_s`, `pop_min_s`, `pop_before_cut_min_s` | `0.15`, `0.6`, `0.5` | `t_pop` = 목소리 시작 + (첫 줄 음절 비율 × 훅 말 길이) − `pop_lead_s`, [`pop_min_s`, `t_cut − pop_before_cut_min_s`] 로 제한, 프레임 칸에 맞춤 |
| `side_margin_px` | `40` | 카드 좌우 여백 |
| `font_regular` | `["assets/fonts/Pretendard-Regular.otf", "C:/Windows/Fonts/malgun.ttf"]` | 카드 글꼴(앞의 것이 없으면 다음) |

타임라인에는 `intro: {style, label, lines, t_pop, t_cut, …모양 숫자}` 로 기록한다(`fx/intro.py` 가 만들고 `render/intro.py` 가 그린다).

## 타임라인에 추가되는 것

```jsonc
"overlays": [{"kind":"sticker","file":"절대경로.png","start":5.589,"end":7.389,
  "anchor":"box_br","size_frac":0.38,"anim":"none","beat":"shock","meme_id":"pepe_shock_01",
  "pop_s":0.18,"overshoot":1.12,"fade_s":0.12,"shake_px":10,"outline_px":6,"margin_px":24}],
"shots": [ …, {"file":"…","kind":"meme","start":24.429,"end":25.629,"motion":"static","bg":[255,255,255],
  "anim":"none","fit":"cover","meme_id":"…","beat":"angry"} ],   // 전체화면 짤
"audio": {"voice":"…","bgm":null,"bgm_db":-22,"sfx_on_cuts":false,
  "sfx":[{"file":"절대경로.wav","t":5.589,"db":-17,"type":"boom","why":"meme_pop","id":"boom_01"}]},
"fx": {"beats":[…],"beats_source":"llm|rules|cache","notes":[…]}
```
렌더러(`render/compose.py`, `render/overlays.py`)는 스티커를 이미지 상자 위·자막 아래에 그린다. 훅 띠보다 위에 겹친다. 전체화면 짤 샷은 겹치는 기존 샷을 잘라 끼우고(0.12초 미만 조각은 짤이 흡수), 잘린 오른쪽 조각은 등장 효과 없이 이어진다.

## 효과음 저작권 점검 (경고만)

- **기록**: `config/sfx_rights.json` — 효과음 id → `level`(high | medium | low), `source`(원본 파일 이름), `note`. 효과음 꾸러미의 manifest 와 따로 둔 파일이라, 사람이 고른 폴더에 같은 꾸러미를 둬도(폴더 이름 `user_meme`) 점검이 된다. **원본 파일 이름만 보고 매긴 판단**이고 소리 지문(Content ID)으로 대조한 것이 아니다(파일 머리 `basis`). `pack`(= `user_meme`) 폴더의 소리만 점검 대상 — 합성음 폴더(`assets/sfx`)에도 `boing_01`·`ding_01` 같은 같은 id 가 있어서다. pack 안인데 기록에 없는 id 는 `unknown_level`(medium).
- **검수 경고**: 검수 단계가 영상 timeline 의 `audio.sfx` 중 medium 이상을 `out/review.json` 의 `sfx_rights: [{t, id, level, note, source}]` 로 남기고, 검수 화면 '확인할 것'에 `효과음 0:24.6 · ding_01` / 저작권 위험 높음(high, 빨강·굵게) · 저작권 주의(medium, 주황) 로 보여 준다. 승인을 막지 않고 효과음을 빼지도 않는다(빼려면 작업 폴더 `fx/sfx_drop.json`).
- **고르기 회피**: `sfx.avoid_rights_risk` — `null`(기본, 고르기 그대로) | `"high"`(high 만 후보에서 뺌) | `"medium"`(medium·high·기록 없는 소리를 뺌). 종류가 비면 그 자리는 건너뛰고 `fx.notes` 에 남는다. 작업마다 `options.fx.sfx.avoid_rights_risk` 로도 덮을 수 있다.
- **목록 보기**: `python -m recap_shorts.fx rights` (꾸러미 전체 표, 위험 높은 순) · `--job work/<작업>` (그 작업 timeline 에 들어간 주의 소리) · `--json`.

## 문장 안 그림 컷은 어절 사이에 (`timing.cuts_at_word_gaps`)

그림은 **문장이 끝날 때** 바꾸고 말 중간에서는 바꾸지 않는다(한 낱말이 컷에 갈리면 듣기 어색하다). 한 문장에 그림이 2~3장 들어가는 것(샷 약 1.4초)은 그대로 두고, **문장 안 컷만** 같은 간격 자리에서 가장 가까운 어절 경계로 옮긴다. 문장 끝 컷은 그대로.

- **켜고 끄기**: `timing.cuts_at_word_gaps` (기본 `true`). `false` 면 예전처럼 문장 구간을 똑같이 나눈다(`timeline.build` 도, 짤 앞 다시 나누기 `fx.apply.fit_before_meme` 도 그대로 — 출력이 글자 하나 안 다르다). 작업마다 `options.fx.timing.cuts_at_word_gaps` 로도 덮는다. `python -m recap_shorts.timeline <작업> --no-word-cuts`.
- **어절 경계 시각** (`recap_shorts/wordcuts.py`): ① 받아쓰기(SenseVoice) 글자 시각 — 문장 소리 조각에 받아쓰기를 돌려 원문 글자와 맞추고 앞·뒤 글자 시각의 가운데를 경계로 쓴다(결과는 작업 폴더 `voice/word_times.json`, 문장 서명이 다르면 다시 돌림. 모델이 없으면 건너뜀) → ② 못 맞춘 경계·모델이 없을 때는 음절 비율(말하는 구간은 목소리 에너지로 잡음, 쉼표 뒤는 쉼 몫 추가) → 그다음 각 경계를 에너지가 가장 낮은 곳으로 조금 옮김(받아쓰기 ±0.05초, 비율 ±0.12초). 음절 비율만으로는 받아쓰기 기준 경계와 0.09초(중앙값)쯤 어긋나서, 받아쓰기가 있을 때만 어절 사이에 확실히 떨어진다.
- **컷 고르기**: 문장에 그림이 N장이면 경계 N-1 곳 — 같은 간격 자리에 가깝고, 샷 0.7초 이상(안 되면 0.55 → 0.4초로 낮춤)·되도록 0.8~2.2초, '안 / 주고'·'할 / 수' 같은 붙여 읽는 말 사이는 피하고(`render/captions.glued`), 쉼이 또렷한 곳·쉼표 뒤를 좋아한다. 0.4초로도 못 고르는 아주 짧은 문장(어절 수 < N-1)은 그 문장만 똑같이 나눈다.
- **목소리 지연**: 그림·자막은 말보다 `picture_lead_s`(0.2초) 먼저 바뀌게 목소리를 늦춰 붙이므로, 컷 시각은 어절 경계 + 지연이다(`word_cuts.lead_s` 0 = 경계에서 바로). 지연을 빼 먹으면 컷이 앞 어절의 끝 음절 안으로 0.2초 일찍 들어간다. `fx.apply.picture_lead(cfg, options)` 가 실제로 지연이 붙을지(fx 꺼짐·짤/효과음 둘 다 꺼짐이면 0)를 알려 준다.
- **자막 카드**: 고른 컷 자리에서 카드도 반드시 끊기고 같은 시각에 바뀐다(`render/captions.make_cards` 의 `breaks`); 카드 안 시각은 컷 사이 구간마다 따로 음절 비율로 나눈다.
- **짤 앞 다시 나누기**: `fit_before_meme(..., word_cuts=)` 가 같은 규칙을 쓴다. 문장 끝 짤(전체화면 샷)의 시작도 그림이 바뀌는 자리라서 `snap_meme_start` 가 가까운 어절 경계로(`word_cuts.snap_meme_s` 0.4초 안, 0 이면 안 옮김) 옮기고, 효과음은 옮긴 시작을 따른다.
- 환경변수 `RECAP_WORDCUTS_ASR=off` 는 받아쓰기를 끈다(시험용). 시험 중(`PYTEST_CURRENT_TEST`)이거나 `RECAP_FX_NO_LLM` 이 있으면 Codex 를 부르지 않고 규칙 라벨만 쓴다.
