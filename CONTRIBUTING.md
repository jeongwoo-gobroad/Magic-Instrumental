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

일반 테스트는 임시 디렉토리의 합성 음원·PDF·MusicXML을 사용합니다. Qt GUI는 offscreen으로 검증하며 기본 테스트에서는 실제 모델 4개를 건너뜁니다. 가중치 이용 조건을 확인하고 실제 모델 시험이 필요하면 Mac에서 다음을 실행합니다.

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

릴리스 전 버전 번호와 CHANGELOG를 맞추고, Ruff·일반 테스트·실제 설치를 확인한 뒤 ZIP/manifest와 Git diff를 검토합니다. 새 태그/릴리스는 그 버전의 소스와 일치해야 합니다. GitHub Actions에는 Mac/Windows 설치 및 테스트 검증을 포함합니다. CI 통과만으로 실제 오디오 장치나 NVIDIA GPU가 검증되었다고 표현하지 마세요.

`main`에 게시된 버전은 두 플랫폼 CI가 모두 통과한 뒤, 아직 같은 버전이 없을 때만 GitHub Release와 `v<버전>` 태그를 생성합니다. 기존 릴리스 파일은 덮어쓰지 않습니다. 릴리스에는 허용 목록 ZIP, 파일 manifest, SHA-256을 첨부합니다. 일반 패키지 검증 작업은 읽기 권한만, 릴리스 작업에만 저장소 쓰기 권한을 부여합니다. PR에서는 릴리스하지 않습니다.

## 이슈와 PR

재현 순서, 기대 동작, 실제 동작, OS/CPU/GPU, 앱 버전을 적어 주세요. 개인 이름·이메일·절대 경로·음원 제목·악보 페이지를 지운 최소 로그를 첨부하세요. 유료 음원·악보·모델 가중치를 이슈나 PR에 업로드하지 마세요. PR에는 해결한 문제와 검증 결과를 설명합니다.


## Portable player development (v2)

See [`.magic` contract and browser architecture](docs/MAGIC_FORMAT.md). All browser sources are in `web_player/`; regenerate `index.html` and `sw.js` with `python tools/build_web.py`. The checked-in vendor distribution must match `vendor/provenance.json` before the documented scheduling adapter is applied.

Install the development requirements into the existing external venv, then run `python -m playwright install chromium webkit`. On macOS use `RUN_WEB_TESTS=1 python -m pytest -q tests/test_web_player.py tests/test_web_audio.py tests/test_web_update.py`; on Windows set `$env:RUN_WEB_TESTS = '1'` before the same command. These opt-in tests launch actual Chromium/WebKit, read synthetic GP/PDF bundles, measure output frequency and media-time rate, exercise count/loop/offline paths, and reject malformed archives. The separate audio module covers playback-session requests, stopped-clock retries and cancellation after background transitions. It injects those platform conditions while retaining real decoding and worklet output; browser automation does not replace physical iPad/Android speaker and lifecycle testing.

`tests/test_web_update.py` exercises the service-worker lifecycle with actual browser registrations and multiple builds: an active verified worker can change while the open document stays intact, and applying an update explicitly loads the new document. Cover safe-apply guards during file/audio preparation, playback, metronome use and Session connection/membership; offline application of a prepared build; failed network/integrity checks; and independent worker scopes. Existing pre-2.4.8 documents have no update controls, so keep their online launch and manual refresh/reopen migration covered. Never substitute a mocked version string for a verified worker/cache lifecycle, and do not call desktop browser coverage a physical home-screen installation test.

`tools/capture_v2_screenshots.py` captures current Qt and web interfaces using original synthetic studies. `tools/build_release.py` explicitly allows the reviewed screenshots, web files and vendor pins. The deployment branch is `mobile-player`; only the static root is intended for public hosting. Runtime user songs, `.magic` archives, score source paths and diagnostics must stay excluded.

CI invokes `tools/run_tests.py`, which runs each test module in a fresh process with a four-minute bound and faulthandler traces after one minute. This isolates native Qt multimedia lifetimes across unrelated modules. Opt-in long-running real-model experiments should use their documented direct pytest invocation instead.


Session tests use `relay/requirements.txt` through the developer requirements; end-user desktop dependencies remain unchanged. Run `RUN_WEB_TESTS=1 python -m pytest -q tests/test_session_player.py` for two-client scheduling, role-specific views, shared files and independent GP parts. Server operation is documented separately in [SESSION_SERVER.md](docs/SESSION_SERVER.md). `tools/capture_session_screenshots.py` captures both actual roles using authored fixtures. Update the versioned direct download in `landing/index.html` when releasing; deploy that page only after the linked release exists.

Deployment maintainers: [separate branch and routing ownership](docs/DEPLOYMENT_BRANCHES.md). Publish landing-page and serverside independently; keep this information out of beginner-facing pages.


## Source updater maintenance

See the v2.2 section of `AI_CODER_README.md` and `tests/test_update.py`. Never exercise an updater against a real user's working folder during tests. Use isolated copies with authored sentinel data and verify byte-for-byte preservation. The release builder emits the full source ZIP and the small `Magic-Instrumental-Updater.zip`; SHA256SUMS covers both. Keep the release manifest and the updater's independent path policy in sync. Publish landing update links only after both release assets exist. Git checkouts are intentionally excluded from the end-user updater.
