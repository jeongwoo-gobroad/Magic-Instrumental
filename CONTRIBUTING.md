# 개발 및 기여

버그 수정, 문서 개선, Windows 실기기 검증을 환영합니다. 큰 동작 변경은 이슈에서 사용 사례를 먼저 설명해 주세요. 기여하는 자체 코드와 문서는 프로젝트와 동일한 **GPL-3.0-only**로 제공되어야 합니다. 제3자 코드·모델의 라이선스와 출처를 보존하세요.

## 개발 환경

먼저 일반 사용자와 동일한 설치 파일을 실행합니다. **전역 패키지 설치와 프로젝트 내부 `.venv` 생성은 금지합니다.** 테스트도 외부 가상환경에서 실행합니다.

macOS, 프로젝트 폴더에서:

```sh
./install.command
VENV_PYTHON="$HOME/.vocal-separator-and-speed-adj/.venv/bin/python"
"$VENV_PYTHON" -m pip install -r requirements-dev.txt
"$VENV_PYTHON" -m pip check
"$VENV_PYTHON" main.py doctor
"$VENV_PYTHON" -m ruff check .
"$VENV_PYTHON" -m pytest -q
```

Windows PowerShell, 기본 경로를 사용할 때:

```powershell
.\install.bat
$VenvPython = "$env:LOCALAPPDATA\Magic-Instrumental\.venv\Scripts\python.exe"
& $VenvPython -m pip install -r requirements-dev.txt
& $VenvPython -m pip check
& $VenvPython main.py doctor
& $VenvPython -m ruff check .
& $VenvPython -m pytest -q
```

경로를 바꿨다면 `$VenvPython`도 해당 경로로 지정합니다. `doctor`는 환경·GPU·FFmpeg·Audiveris 위치를 출력하며 개인 경로를 포함하므로 그대로 게시하지 마세요.

일반 테스트는 임시 디렉토리의 합성 음원·PDF·MusicXML을 사용합니다. Qt GUI는 offscreen으로 검증하며 기본 테스트에서는 실제 모델·메모리 조합 10개를 건너뜁니다. 가중치 이용 조건을 확인하고 실제 모델 시험이 필요하면 Mac에서 다음을 실행합니다.

```sh
RUN_MODEL_TESTS=1 "$VENV_PYTHON" -m pytest tests/test_integration.py -v
```

Windows에서는 `$env:RUN_MODEL_TESTS = '1'`을 설정한 뒤 같은 pytest 파일을 실행합니다. 이 테스트는 모델을 다운로드할 수 있으며 분리 품질에 대한 객관적 벤치마크는 아닙니다. GPU 실기기 검증 결과는 OS·장치·드라이버·모델·테스트 범위를 명시하세요.

## 구조와 변경 규칙

[AI_CODER_README.md](AI_CODER_README.md)는 모듈 구조, 캐시 불변 조건, 악보 시간 단위, MPS 보정 및 알려진 한계를 설명합니다. AI Coder도 이 문서와 [AGENTS.md](AGENTS.md)를 먼저 읽어야 합니다.

- 원본 음원·악보·검토된 시간표를 보존하세요. 테스트는 개인 자료를 필요로 하면 안 됩니다.
- 소스 SHA-256과 처리 서명으로 캐시를 구분합니다. 분리 의미가 달라지면 `PIPELINE_VERSION`을 올립니다.
- 원곡 시간과 배속 적용 후 시간을 혼동하지 마세요. OMR·정렬·스크롤 경로 계산은 작업 프로세스에서만 합니다.
- GUI 주 스레드에 모델 로딩·분리를 추가하지 마세요. Windows 취소는 Java를 포함한 자식 프로세스까지 처리합니다.
- 사용자 동작이 달라지면 README, 사용 안내, AI 인계, CHANGELOG를 갱신합니다.

### 캐시 폴더 설정과 작업 프로세스

3.0.4부터 `Paths.audio_cache`와 `Paths.score_cache`를 각각 설정할 수 있습니다. 기본값은 모두 기존 `processed`이며, `Paths.processed` 자체는 UI·곡별 설정과 악보 연결 정보를 위한 고정 위치로 유지합니다. 폴더 선택은 `processed/cache-locations.json`에 원자적으로 저장하고 모든 CLI 작업 프로세스가 같은 프로젝트 루트의 설정을 읽습니다. 두 폴더의 유효성과 쓰기 가능 여부를 확인하기 전에 설정을 교체하지 마세요.

음원 분리·조합·키 변경·캐시 탐색은 음원 폴더를, 악보 분석·OMR 중간 파일·GP 표시·전체 파트 준비는 악보 분석 폴더를 사용합니다. 가져온 `.magic`은 무결성 검증된 하나의 묶음으로 음원 폴더에 유지합니다. 현재 열린 파일과 새 작업의 저장 위치를 구분하고, 경로 저장만으로 기존 파일을 이동·삭제·재분석하지 않습니다. 설정 경로와 실제 파일 경로는 개인 정보이므로 공개 테스트 결과·배포물에 포함하지 마세요.

변경 검증에는 임시 디렉토리에서의 폴더 저장·재실행, 실제 작업 프로세스의 저장 위치, 잘못된 폴더/쓰기 실패 시 기존 설정 유지, 기존 `processed` 캐시 재사용을 포함합니다. GUI에서는 폴더 선택·직접 입력·기본 위치·저장하지 않고 닫기와 실제 상단 재생 선택란의 0/1/여러 음원 상태를 확인하세요. 체크박스 개수와 실제 저장된 모델 출력 개수가 같다고 가정하지 마세요.


## 분리 옵션 CLI

```sh
python main.py separate --input targets/example.wav --model auto --variant no_guitar
python main.py separate --input targets/example.wav --model auto --all-variants
python main.py separate --input targets/example.wav --model auto --variants no_vocals no_guitar no_vocals_guitar --variant no_vocals_guitar
python main.py separate --input targets/example.wav --model hyperace --variant no_vocals --offline
python main.py magic-import --input practice.magic
```

기본 옵션은 `no_vocals`, `vocals`, `no_guitar`, `guitar`, `no_drums`, `drums`, `no_bass`, `bass`, `no_keys`, `keys`, `no_piano`, `piano`, `no_synth`, `synth`, `original`입니다. `keys`는 건반 계열, `piano`는 피아노, `synth`는 실험적인 전자 합성음 분리이며 서로 겹칠 수 있습니다. 자동 추천은 Leap·SW·Mega를 파트별로 선택합니다. `--events` 결과는 기존 `instrumental` 경로와 함께 `selected_variant`, `variants`, 원본 frame/rate 정보를 제공합니다. `instrumental`은 호환성을 위해 유지한 이름이며 현재 선택한 음원의 경로입니다. 모델별 출력·캐시 변경과 실제 검증 범위는 [모델 문서](docs/SEPARATION_MODELS.md), 저장 계약은 [형식 문서](docs/MAGIC_FORMAT.md)를 확인하세요.

조합은 `no_vocals_guitar`처럼 제거 대상을 고정 순서(보컬·기타·드럼·베이스·키보드·피아노·신시사이저)로 연결한 ID를 명시합니다. CLI의 `--variants`는 명시한 항목만 요청하며 개별 ID 선택만으로 새 조합 추론을 추가하지 않습니다. GUI는 없는 반주를 두 개 이상 체크하면 해당 조합 ID도 명시적으로 추가합니다. 같은 모델을 원곡에서 한 번 실행하는 개별 준비와 달리, 새 조합은 앞 단계 잔여음원을 입력으로 추가 추론할 수 있습니다. 완성 조합은 별도 서명과 원곡 해시로 캐시합니다.

3.0.5의 일반 GUI `.magic` 저장은 `include_original: true`를 요청해 원곡 하나와 선택한 분리 음원을 함께 담습니다. 원곡은 실제 요청 `audio`에서 모델 실행 없이 준비하고, 분리 음원들과 동일한 키·공통 음량·시간 기준·음질을 적용합니다. 분리 음원을 모두 해제한 원곡 전용 파일도 허용합니다. 이 플래그를 생략한 기존 API 요청의 정확한 선택 목록과 형식 1~5 호환성은 유지합니다. 같은 곡의 캐시는 기존처럼 원본 전체 SHA-256을 기준으로 모으며, 가져온 `.magic`의 선언 해시로 일반 분리 캐시를 채우지 않습니다. 조합의 표시 이름은 `(보컬 + 기타) 없는 반주`처럼 제거 대상 전체를 괄호로 묶습니다. [요청 및 저장 계약](docs/MAGIC_FORMAT.md)을 함께 확인하세요.

## 의존성 갱신

직접 의존성은 `requirements.txt`, 플랫폼 전체 잠금은 `requirements-macos.lock.txt`와 `requirements-windows.lock.txt`, Windows CUDA wheel은 `requirements-cuda.txt`입니다. 개발 도구는 `requirements-dev.txt`입니다. Python/uv 버전과 uv 배포물 SHA-256은 `installers/runtime.json`에 고정합니다. 최신 버전 문자열만 교체하지 말고 양쪽 플랫폼 wheel 제공 여부와 실제 설치를 확인하세요.

Audiveris/영어 데이터/원본 라이선스의 버전·다운로드 SHA-256·소스 링크는 `installers/omr.json`에 고정합니다. 양쪽 설치기는 합성 악보를 생성해 Java 실행과 영어 단어 인식을 실제로 검사합니다. Windows는 읽기 전용 MSI 데이터베이스와 CAB 추출만 사용하며 `msiexec` 설치·레지스트리 등록을 실행하지 않습니다. 이 검사 통과는 임의의 실제 악보 인식 정확도나 오디오 장치 검증을 뜻하지 않습니다. 설치 구조/검사 의미가 달라지면 `omr_runtime.INSTALLER_REVISION`도 검토하세요.

공식 uv의 `pip compile`로 Python 3.12와 `aarch64-apple-darwin` / `x86_64-pc-windows-msvc`를 각각 해석하고 `--only-binary :all:`을 사용합니다. Windows CPU/CUDA wheel을 구분해 확인하세요. 앱에는 uv 런타임 import 의존성이 없습니다.

## 공개 배포

```sh
"$VENV_PYTHON" tools/build_release.py --check
"$VENV_PYTHON" tools/build_release.py
```

배포 도구는 **명시한 소스 파일만** ZIP과 공개 파일 manifest에 넣습니다. 음원·악보·캐시·모델·로그·개인 Windows 설정은 넣지 않습니다. 프로젝트 폴더 전체를 압축하거나 `git add .`로 검토 없이 게시하지 마세요. ZIP에 `.command` 실행 권한을 기록합니다.

릴리스 전 버전 번호와 CHANGELOG를 맞추고, Ruff·일반 테스트·실제 설치를 확인한 뒤 ZIP/manifest와 Git diff를 검토합니다. 데스크톱의 `practice_player.__version__`와 웹의 `web_player/template.html` 안 `magic-version`을 같은 버전으로 맞춘 뒤 `tools/build_web.py`를 실행합니다. `tests/test_release.py`가 템플릿·생성 HTML·화면 하단·서비스 워커의 버전 일치와 HTML/워커의 빌드 식별자·SHA-256 연결을 검사합니다. 새 태그/릴리스는 그 버전의 소스와 일치해야 합니다. GitHub Actions에는 Mac/Windows 설치 및 테스트 검증을 포함합니다. CI 통과만으로 실제 오디오 장치나 NVIDIA GPU가 검증되었다고 표현하지 마세요.

`main`에 게시된 버전은 두 플랫폼 CI가 모두 통과한 뒤, 아직 같은 버전이 없을 때만 GitHub Release와 `v<버전>` 태그를 생성합니다. 기존 릴리스 파일은 덮어쓰지 않습니다. 릴리스에는 허용 목록 ZIP, 파일 manifest, SHA-256을 첨부합니다. 일반 패키지 검증 작업은 읽기 권한만, 릴리스 작업에만 저장소 쓰기 권한을 부여합니다. PR에서는 릴리스하지 않습니다.

## 이슈와 PR

재현 순서, 기대 동작, 실제 동작, OS/CPU/GPU, 앱 버전을 적어 주세요. 개인 이름·이메일·절대 경로·음원 제목·악보 페이지를 지운 최소 로그를 첨부하세요. 유료 음원·악보·모델 가중치를 이슈나 PR에 업로드하지 마세요. PR에는 해결한 문제와 검증 결과를 설명합니다.


## Portable player development (v3)

See [`.magic` contract and browser architecture](docs/MAGIC_FORMAT.md). All browser sources are in `web_player/`; regenerate `index.html` and `sw.js` with `python tools/build_web.py`. The checked-in vendor distribution must match `vendor/provenance.json` before the documented scheduling adapter is applied.

Install the development requirements into the existing external venv, then run `python -m playwright install chromium webkit`. On macOS use `RUN_WEB_TESTS=1 python -m pytest -q tests/test_web_player.py tests/test_web_follow.py tests/test_web_audio.py tests/test_web_update.py tests/test_web_variants.py`; on Windows set `$env:RUN_WEB_TESTS = '1'` before the same command. These opt-in tests launch actual Chromium/WebKit, read synthetic GP/PDF bundles, measure output frequency and media-time rate, exercise count/loop/offline paths, and reject malformed archives. The separate follow module covers continuous PDF/GP pages, viewport-centered current measures, page/system transitions, navigation jumps, manual-follow restoration and zoom/resize behavior. The variants module checks actual decoded spectra, transactional option replacement, retained position/key/rate/range/part, baked common gain, failed replacements and independent Session audio choices. Native pipeline/export/import and worker-driven Qt tests are in `test_cli_variants.py`, `test_gui_variants.py`, `test_magic_variants.py` and `test_magic_import.py`. The separate audio module covers playback-session requests, stopped-clock retries and cancellation after background transitions. It injects those platform conditions while retaining real decoding and worklet output; browser automation does not replace physical iPad/Android speaker and lifecycle testing.

`tests/test_web_update.py` exercises the service-worker lifecycle with actual browser registrations and multiple builds: an active verified worker can change while the open document stays intact, and applying an update explicitly loads the new document. Cover safe-apply guards during file/audio preparation, playback, metronome use and Session connection/membership; offline application of a prepared build; failed network/integrity checks; and independent worker scopes. Existing pre-2.4.8 documents have no update controls, so keep their online launch and manual refresh/reopen migration covered. Never substitute a mocked version string for a verified worker/cache lifecycle, and do not call desktop browser coverage a physical home-screen installation test.

`tools/capture_v2_screenshots.py` captures current Qt and web interfaces using original synthetic studies, including several checked and prepared audio choices. Native captures include light/dark practice views and the editable cache-folder dialog in `docs/images/cache-options.png`. Use `--desktop-only` when browser runtimes are unavailable; this updates only the native screenshots and is not browser validation. Add `--qa-dir` with a scratch path to inspect small-window light/dark layouts without adding diagnostic images to public docs. The variants/follow browser tests also save current sidebar-layout evidence when `MAGIC_RENDER_CHECK_DIR` is set. `tools/build_release.py` explicitly allows the reviewed screenshots, web files and vendor pins. The deployment branch is `mobile-player`; only the static root is intended for public hosting. Runtime user songs, `.magic` archives, score source paths and diagnostics must stay excluded.

CI invokes `tools/run_tests.py`, which runs each test module in a fresh process with a four-minute bound and faulthandler traces after one minute. This isolates native Qt multimedia lifetimes across unrelated modules. A failed or timed-out module is recorded while the remaining isolated modules still run; any recorded failure makes the overall run fail. This preserves every gate and provides complete diagnostics instead of hiding later browser checks behind an earlier failure. Opt-in long-running real-model experiments should use their documented direct pytest invocation instead.

The shared single-page browser fixture starts each Chromium/WebKit process once per test module, then creates and closes a fresh browser context for every test. Cookies, storage, service workers, permissions, pages and audio contexts remain isolated between cases. Reusing the browser processes avoids repeated startup costs in the 50-case player module without changing test selection, engine combinations, assertions, retries or the four-minute bound. The pool closes both engines at module teardown. Multi-audio variant tests use `fresh_page` to retain their original per-case browser process lifetime; Session tests that explicitly manage multiple participants also retain their own browser lifecycle.

Browser runs emit each case's start and setup/call/teardown result immediately. Failed reports include their original traceback before continuing, so a later native cleanup stall or module timeout cannot hide the first failure. The Session readiness observer also covers readiness cancellation and re-preparation, recording which participant's state is being awaited and the visible readiness text. These diagnostics do not alter test actions, assertions or time limits.

With `MAGIC_RENDER_CHECK_DIR` set, the shared `page` and `fresh_page` fixtures write `browser-diagnostics/<engine>-<nodeid SHA256 prefix>.json` during cleanup; the prefix is 12 characters and the full test node ID is inside the file. These retain the latest 80 browser lifecycle/console events and 120 installation-file HTTP events, including intentional fixture closure, so a closed WebKit page can still leave evidence. A service-worker readiness failure adds worker/controller/build/UI state, the latest 80 registration/update events, and registration/cache queries each bounded to one second to the original exception and artifact. The observer returns the original registration/update promises. Existing 30-second readiness waits, module bounds, browser lifetimes, assertions and retry policy remain unchanged.


Session tests use `relay/requirements.txt` through the developer requirements; end-user desktop dependencies remain unchanged. Run `RUN_WEB_TESTS=1 python -m pytest -q tests/test_session_player.py` for two-client scheduling, role-specific views, shared files and independent GP parts. Server operation is documented separately in [SESSION_SERVER.md](docs/SESSION_SERVER.md). `tools/capture_session_screenshots.py` captures both actual roles using authored fixtures. Update the versioned direct download in `landing/index.html` when releasing; deploy that page only after the linked release exists.

With `MAGIC_RENDER_CHECK_DIR` set, the scheduled Session test saves `session-readiness-<engine>.json` on success or failure around the held-resume/background/foreground preparation sequence. It records both participants' before/after state, up to 160 recent audio/readiness/visibility events and samples, probe ages/RTTs, and page errors. A failure identifies the participant being awaited and summarizes both final states in the original exception; full history stays in the artifact. Invite codes, authentication and media identifiers are excluded. The observer preserves original action promises, clicks, readiness assertions and the 30-second wait; it does not retry or alter production audio, clock or cancellation policy.

The same bounded observer records `session-readiness-<engine>-loop-count-play.json`, `-shared-variants-file.json` and `-shared-combined-file.json` for repeated-range playback and Session downloads. These include decoded frame counts, page preparation, loop/count settings and pending command timing. The original 7-second playback and 30-second file-ready waits remain unchanged, with no automatic retries.

Deployment maintainers: [separate branch and routing ownership](docs/DEPLOYMENT_BRANCHES.md). Publish landing-page and serverside independently; keep this information out of beginner-facing pages.


## Source updater maintenance

See the v2.2 section of `AI_CODER_README.md` and `tests/test_update.py`. Never exercise an updater against a real user's working folder during tests. Use isolated copies with authored sentinel data and verify byte-for-byte preservation. The release builder emits the full source ZIP and the small `Magic-Instrumental-Updater.zip`; SHA256SUMS covers both. Keep the release manifest and the updater's independent path policy in sync. Publish landing update links only after both release assets exist. Git checkouts are intentionally excluded from the end-user updater.
