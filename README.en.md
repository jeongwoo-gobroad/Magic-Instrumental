# Magic-Instrumental

Make an instrumental on your computer, choose a score or Guitar Pro part, then practise at your own pace. A Python desktop app for Apple Silicon macOS and Windows, with a separate offline tablet player for iPad and Android.

[한국어 / full beginner guide](README.md) · [Tablet guide](web_player/README.md) · [Changelog](CHANGELOG.md) · [Developer guide](CONTRIBUTING.md)

![Desktop app with a project-authored GP study](docs/images/desktop-light.png)

## Install and open

Download the latest **Magic-Instrumental-version.zip** from [Releases](https://github.com/jeongwoo-gobroad/Magic-Instrumental/releases/latest) and fully extract it. Double-click `install.command` on Mac or `install.bat` on Windows. Wait for completion. For everyday use, double-click `start.command` or `start.bat`.

The installer prepares Python, dependencies, FFmpeg, Audiveris, Java, English OCR data and GP engraving tools without a preinstalled Python or administrator access. All project packages live in an external virtual environment. An internet connection is needed for initial setup and model downloads. Supported desktop targets: macOS 14+ Apple Silicon and Windows 10/11 x64. The interface is Korean.

## Your first practice session

1. Put a lawfully obtained recording into `targets/`.
2. Refresh the list, choose the song, and prepare its instrumental. Identical file contents reuse verified cached results.
3. Open a PDF or GP3/GP4/GP5 score. For GP, choose the instrument **by name**; no companion PDF is required.
4. Choose score zoom and panel width, run score pre-analysis, then review the timing.
5. Play at 0.25–2× speed, change the instrumental key by ±12 semitones, jump by measures or repeat a selected range. Optional drum counts and the independently adjustable metronome follow the original-media clock.
6. Save a `.magic` practice file and open it in the tablet player. GP-derived files include the selected part's rendered pages and timeline, so mobile playback requires no score recognition or GP software.

Applied desktop key changes are included in the exported audio. The web player can add another ±12 semitones; the default is zero additional shift. Original printed notes/TAB are not transposed. PDF recognition and audio alignment can require manual correction. GPX and GP6/7/8 are currently unsupported. Drum parts use fixed-BPM timing.

**Desktop metronome alone:** uncheck ‘음원과 연동’ (link to audio), then press its ▶ button. No song is required. An always-visible hint and status show standalone playback, waiting for audio, linked playback, or other waiting states; mute and zero volume are also indicated.

For iPad/Android, [open Magic Player](https://jeongwoo-gobroad.github.io/Magic-Instrumental/) in Safari/Chrome and add it to the home screen. After offline preparation, it works without internet; songs remain local. Direct HTML opening on iPad's Files app is not reliable. Keep the player visible during practice; backgrounding pauses playback. [Tablet instructions](web_player/README.md)

**To use the mobile metronome alone, uncheck ‘반주와 함께’ (with accompaniment), then turn it on.** No song file is required. With that box checked, clicks wait until the accompaniment plays; the header shows the waiting state.

## Rights and attribution

**Recordings and scores are not supplied.** Obtain your own materials lawfully, paying applicable charges. Buying a recording or score does not grant permission to redistribute its original, separated instrumental or `.magic` bundle.

Original project code/docs: GPL-3.0-only. Models and dependencies retain their own licenses. PDF recognition uses Audiveris; GP parsing uses PyGuitarPro; engraving uses Verovio; mobile rate/pitch processing uses Signalsmith Stretch. See [third-party notices and model provenance](THIRD_PARTY_NOTICES.md). Screenshots contain only project-authored synthetic music.

Automated checks distinguish desktop installation, browser execution, and real hardware validation; see [developer documentation](CONTRIBUTING.md). No claim of universally best separation, perfect recognition, or testing on every physical device is made.
