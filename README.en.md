# Magic-Instrumental

A local Python desktop practice player for macOS and Windows: remove vocals, play the instrumental at 0.25–2× speed with pitch preservation, follow a pre-analyzed PDF score, and practice with a synchronized metronome. The application UI is currently in Korean.

[한국어 / full guide](README.md) · [User guide](docs/USER_GUIDE.md) · [Changelog](CHANGELOG.md) · [Contributing](CONTRIBUTING.md)

## Screenshots

![Actual application: instrumental controls, synchronized metronome and PDF score](docs/images/player-overview.png)

![Actual application following a score at 200 percent zoom](docs/images/score-zoom.png)

Captured from the running Qt application with project-authored synthetic demo audio and a demo score. No personal or commercial recording or sheet music is shown.

## Install and run

Download **Code → Download ZIP**, fully extract it, then double-click:

| Platform | Install / update | Launch |
|---|---|---|
| Apple Silicon, macOS 14+ | `install.command` | `start.command` |
| Windows 10/11 x64 | `install.bat` | `start.bat` |

No preinstalled Python is required. The installer downloads checksum-pinned uv, managed Python 3.12.14, and pinned dependencies into a user-owned external virtual environment. It does not install global Python packages or change the system PATH. An internet connection and several GB of free space are needed for installation. Model weights download on first use.

On macOS, the environment is `~/.vocal-separator-and-speed-adj/.venv` (the folder name is retained for upgrade compatibility). If macOS blocks opening a downloaded command, verify the download source and follow the per-file Open / Privacy & Security prompt. If executable permissions are missing, run `zsh install.command` and `zsh start.command` in Terminal.

On Windows, the installer creates `venv-location.windows.txt` from its example. To choose a different location before installation, copy `venv-location.windows.example.txt` to that name and enter one absolute path outside this project. Default: `%LOCALAPPDATA%\Magic-Instrumental\.venv`. Use `install.bat -Cpu` or `install.bat -Cuda` to override automatic NVIDIA detection. Drivers are installed separately. Intel Macs and Windows ARM are not supported by this release.

## Practice

Put your audio in `targets/`. Refresh, select it, prepare the track, then play. Instrumentals are cached by the complete file SHA-256 and processing settings; changing a filename does not require separation again. Common FFmpeg audio formats are supported; DRM-protected downloads are not.

Put a matching PDF in `score_targets/`, for example `Practice Song.mp3` and `Practice Song.pdf`. Resize the right-hand score panel and choose the zoom **before** analyzing. Repeat signs, endings, D.C./D.S./coda navigation, audio alignment, and scrolling paths are computed ahead of playback. Adjustable measure skips follow performance order, including repeat visits.

PDF viewing is included. PDF music recognition additionally needs [Audiveris](https://audiveris.github.io/audiveris/_pages/tutorials/install/binaries/) and English OCR data (`eng`), installed separately. A matching MusicXML file can replace recognition. Recognition and alignment are estimates requiring review; they are not guaranteed automatic transcription.

The metronome supports independent use or synchronization using the original BPM multiplied by playback speed, with beat circles, a time signature, and a first-beat offset. It uses a constant entered tempo, not automatic tempo tracking.

## Media, privacy and licensing

**No recordings or scores are included. Users must obtain and supply their own legally authorized audio and sheet music, paying the required price where applicable.** Freely distributed works must also be used under their terms. Purchasing a copy does not automatically grant redistribution rights. Do not share copyrighted originals or derived stems without permission.

The application processes media locally. Dependency and model downloads contact their distributors; audio and scores are not uploaded by this app. Media, caches, model weights, and personal path settings are excluded from source releases. Any cloud-folder synchronization is controlled by the user's storage provider.

Original project code and documentation are **GPL-3.0-only**; see [LICENSE](LICENSE). **Models and dependencies retain their own licenses**, and GPL does not license your recordings or scores. The default HyperACE v2 model has no explicit weight license in the checked author card; do not assume commercial or redistribution permission. The alternative Mel-Band RoFormer deux weights are CC BY-NC 4.0. See [third-party notices](THIRD_PARTY_NOTICES.md).

Apple Silicon installation and actual MPS inference have been exercised. Windows hardware audio and NVIDIA inference still need validation. Automated tests use synthetic media; this project makes no universal separation-quality or score-recognition guarantees.
