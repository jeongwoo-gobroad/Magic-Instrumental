# Magic-Instrumental

Separate vocals, guitar, drums, bass or keyboards on your computer, choose a score or Guitar Pro part, then practise at your own pace. A Python desktop app for Apple Silicon macOS and Windows, with a separate offline tablet player for iPad and Android.

Current version: **3.0.3** · Mac / Windows / web player

[한국어 / full beginner guide](README.md) · [Tablet guide](web_player/README.md) · [Changelog](CHANGELOG.md) · [Developer guide](CONTRIBUTING.md)

![Desktop app with a project-authored GP study](docs/images/desktop-light.png)

The large **재생** (Play) button is on the left of the fixed transport row at the top, beside seek, the clock and the audio selector. The restart button remains in the left panel. The whole row stays visible when the options sidebar is scrolled or hidden. In a small window, scroll down the left side to reach the save buttons. Drag the divider to widen the score. When preparing an original PDF/GP, press **준비 도구 접기** in the score panel to hide the setup controls and make the page taller, then press it again to restore them. Imported `.magic` scores automatically use compact controls with part selection visible. On a phone, open your .magic file first, then tap **재생 조절로** for playback controls or **악보로** for the score.

## Install and open

Download the [3.0.3 installation ZIP](https://github.com/jeongwoo-gobroad/Magic-Instrumental/releases/download/v3.0.3/Magic-Instrumental-3.0.3.zip) and fully extract it. Double-click `install.command` on Mac or `install.bat` on Windows. Wait for completion. For everyday use, double-click `start.command` or `start.bat`.

**On first launch on Mac**, double-click `install.command`. If macOS blocks it because it cannot verify the developer, first confirm that the ZIP came from this project's official release. Open **Apple menu → System Settings → Privacy & Security**, scroll to **Security**, and choose **Open Anyway** for that file. Confirm when prompted. If macOS later blocks `start.command` on its first launch, repeat these steps for that file. [Apple's guide in Korean](https://support.apple.com/ko-kr/102445)

The installer prepares Python, dependencies, FFmpeg, Audiveris, Java, English OCR data and GP engraving tools without a preinstalled Python or administrator access. All project packages live in an external virtual environment. An internet connection is needed for initial setup and model downloads. Supported desktop targets: macOS 14+ Apple Silicon and Windows 10/11 x64. The interface is Korean.

**If Windows freezes or closes while checking for updates:** close the app and error window, double-click the existing `update.bat`, then reopen with `start.bat`. Update to v2.3.1 or later; no in-app button is needed. Keep your audio, scores and processed results. Reinstallation is not required for this fix.

## Update without moving your songs

**3.0.3 limits the playback selector to currently available saved audio and moves audio and score-analysis cache locations into a separate desktop details dialog.** Check two or more **없는 반주** (without this part) options to prepare the individual versions plus one combined backing track. For example, vocals and guitar also produce **보컬 + 기타 없는 반주**, selected initially. The combination is cached and can be included as one audio choice in a `.magic` file. Sequential separation needs extra processing the first time; a verified completed combination can be reused. Existing 2.4.x and 3.0.x installations use the regular updater below.

Old recordings, scores, reviewed timings and `.magic` files stay usable. An older file containing only separate vocal-removed and guitar-removed versions has no jointly removed track. Prepare that combination from the original recording, then export a new `.magic` including it; score reanalysis is unnecessary. Files containing combined tracks require desktop/web player **3.0.2 or later**.

On Windows, the key controls become available as soon as the first instrumental is ready. Cancelling an analysis or separation also stops its helper processes. Completed instrumentals remain available on the next launch.

At startup, the desktop shows your current version and checks the latest stable release in the background. When available, use **변경 내용** (release notes) and **종료하고 업데이트** (close and update). The updater waits for the app to exit, then preserves your files and settings. Reopen with `start` after completion. Offline checks never block practice, and **다시 확인** retries. Finish any analysis/export before updating. Nothing installs automatically.

Close the app, then double-click **`update.command` (Mac)** or **`update.bat` (Windows)** in your existing app folder. Wait for completion and use the same `start` file. The updater checks required tools in your existing external environment automatically.

If your older version has no update file, download and fully extract the [small updater ZIP](https://github.com/jeongwoo-gobroad/Magic-Instrumental/releases/latest/download/Magic-Instrumental-Updater.zip). Double-click its update file and select your **existing app folder containing `start` and `targets`**. Future updates use the update file added to that original folder.

Recordings, scores, processed instrumentals, reviewed timings, models, `.magic` exports and personal settings stay in place. Do not delete the old folder. Internet is required. If interrupted, run the updater again to recover and retry. Developer Git checkouts are not updated by this tool. [Detailed steps (Korean)](docs/UPDATING.md)

## Your first practice session

1. Put a lawfully obtained recording into `targets/`.
2. Refresh the list, choose the song, check one or more audio options under **준비할 음원**, then press **체크한 음원 준비**. Matching recording bytes, model and settings reuse verified cached results.
3. Open a PDF or GP3/GP4/GP5 score. For GP, choose the instrument **by name**; no companion PDF is required.
4. Choose score zoom and panel width, run score pre-analysis, then review the timing.
5. Play at 0.25–2× speed, change the instrumental key by ±12 semitones, jump by measures or repeat a selected range. Optional drum counts and the independently adjustable metronome follow the original-media clock.
6. Press **PC·태블릿으로 가져가기 · .magic 저장**, select included audio options, the initial option and audio quality, then press **저장 위치 선택…** (choose save location). Open the saved `.magic` in the tablet player. GP-derived files include every part's rendered pages and a shared timing reference, so mobile playback requires no score recognition or GP software.

Applied desktop key changes are included in the exported audio. The web player can add another ±12 semitones; the default is zero additional shift. Original printed notes/TAB are not transposed.

**New exports use MP3 at 256 kbps by default.** The **연습 파일 음원과 음질** dialog also offers MP3 at 128, 192 or 320 kbps, and **WAV · 무손실** (lossless WAV). The desktop remembers your confirmed choice for the next export. MP3 reduces the embedded accompaniment size; the reduction in the whole `.magic` depends on the score pages and other contents. Playback still decodes the audio, so a smaller file does not guarantee lower memory use.

Existing `.magic` files containing WAV remain playable and are never converted automatically. Use **web player 3.0.2 or later** for files containing combined removal tracks; older MP3/WAV formats remain supported. Follow the [web update steps below](#home-screen-installation-and-web-updates), then check the footer version before choosing your file.

**Measure highlighting is on by default.** PDF and GP scores show a green outline and light translucent fill over the whole current measure. Use **마디 강조** (measure highlight) on the desktop or web player to hide it immediately or show the current measure again without reanalysis. The desktop app and each web browser remember their own choice across songs and parts, including Session use.

Automatic following stacks PDF and GP pages vertically and centers the active measure in the score viewport. Nearby forward moves between systems and pages are smooth. Moves longer than the viewport height, explicit seeks, repeats and coda jumps return promptly to the target without scrolling through intervening pages. Blank space above and below the score lets the first and last measures reach the center too. Simultaneous staves stay together; lower the zoom if a full score is taller than the viewport.

How much upcoming music is visible depends on zoom, viewport height and the original page margins. The next system or page may still be outside the viewport. Reduce zoom or give the score more vertical space; on desktop, try **한 페이지** (fit page) or fold the preparation controls.

On the desktop, scrolling or turning pages manually disables following. Turn **자동 넘김 · 스크롤** back on to center the current playback measure immediately; the web control is **자동 따라가기**. Saved measure positions keep centered following available after zooming, resizing or folding the preparation controls without reanalysis. Highlighting remains independent. Measure numbers and repeat visits continue updating with either setting off, and resuming following while paused does not start audio.

**If highlighting is on but an older analysis has no measure positions:** open score pre-analysis and select **기존 마디 시간표 유지 · 화면 이동 경로만 분석** (keep the existing measure timeline and rebuild only the display path). This adds measure positions while preserving your corrected timings. It does not rerun note recognition or audio alignment, and the original score stays unchanged.

**We recommend Guitar Pro scores in `.gp3`, `.gp4` or `.gp5` format.** The app reads notes and measures directly from these files and lets you choose a part by name.

**If you have MusicXML:** use **MusicXML 선택** in the analysis dialog to choose `.musicxml`, `.mxl` or `.xml`, or place a file with the same name beside the PDF. The app checks the explicit source, matching file, reviewed source, verified previous recognition and genuine PDF MusicXML attachment before running Audiveris. Watermark XML is not musical notation. Errors in a supplied source remain visible; the app does not silently replace that source with measure/BPM estimates. **정렬** offers automatic selection, estimated note/audio alignment, or fixed-BPM timing.

**PDF full scores and combined notation/TAB:** enter quarter-note BPM, meter and lead-in, then run score pre-analysis. In automatic mode, when no supplied MusicXML is available and OMR recognition fails, the app can read shared measure boundaries directly from 4-, 5- and 6-line staves and estimate timing from your settings. Review the estimated timing. The original full score stays together, with no part separation or TAB numeral-to-pitch transcription. Choose a zoom that fits the simultaneous staves.

When the app falls back to PDF measure/BPM timing, it follows printed order unless you supply a performed measure route. Missing or conflicting repeat/coda instructions stop analysis and ask for a corrected route or MusicXML; they are not replaced with a successful straight-through timeline. Pickups, meter changes and tempo changes need timing review. Existing note/audio alignment still requires Audiveris or corresponding MusicXML/GP and does not guarantee full-score or TAB transcription. PNG/JPG/JPEG score images are not direct inputs. Image-only or scanned PDFs can fail because of resolution, skew, blur or ambiguous system grouping. GPX and GP6/7/8 are currently unsupported. GP drum parts use fixed-BPM timing.

**If repeat/coda behavior changed after an update:** reopen the score. When the latest result is an unreviewed automatic fallback from an older analysis policy, the app reconnects an earlier reviewed timeline for the exact same audio and PDF if one remains available. Otherwise run score pre-analysis again. Previous recognition sources are reused only when their contents match the saved analysis. Existing files, reviewed timing and explicit routes are preserved. Save a new `.magic` after changing the timeline so the tablet receives the correction.

**If following does not resume when you turn it back on:** update to 3.0.0 or later. Existing measure positions support centered following at the current zoom and viewport size; this viewing change alone requires no new `.magic` export. Use **악보 사전 분석 → 기존 마디 시간표 유지 · 화면 이동 경로만 분석** only to add missing positions to an older analysis. This preserves reviewed timing without rerunning note recognition or audio alignment. To reread repeat/coda signs, clear that checkbox and run full analysis. A combined timing/scroll job switches to the new result only after both stages succeed.

**Desktop metronome alone:** uncheck ‘음원과 연동’ (link to audio), then press ‘메트로놈 켜기’ (turn metronome on). No song is required. An always-visible hint and status show standalone playback, waiting for audio, linked playback, or other waiting states; mute and zero volume are also indicated.

On iPad, [open Magic Player](https://jeongwoo-gobroad.github.io/Magic-Instrumental/) in Safari or Chrome; on Android, use Chrome. Open the web address first, tap **.magic 파일 열기** (open a .magic file), then choose the file saved on your computer. Other file types may also appear in the picker; an invalid file produces an error when opened.

**If file opening stopped responding in iPad Chrome or the home-screen app:** follow the web update steps below and check that the footer shows **3.0.3 or later**. Selecting a file shows loading progress. Wait for the song title, duration and score, then press **재생** (Play). If audio preparation fails, the loaded song stays available so you can press Play again. Existing files need no rename or new export for this correction.

**If the song and score open but your iPad remains silent:** update the web player to **3.0.3 or later**. Supporting browsers receive an explicit music-playback audio-session request when you press Play, enable the metronome, or press Session Ready. The file-loading correction from 2.4.6 and the playback-session setup introduced in 2.4.7 remain in place. If sound preparation fails, press Play or Session Ready again without reopening the file; the selected part, playback position and practice settings stay in place. Returning from another app does not restart playback automatically. Check the device volume and selected audio output as well.

### Home-screen installation and web updates

Open [Magic Player](https://jeongwoo-gobroad.github.io/Magic-Instrumental/) first, then add **that player page** to your home screen.

| Device and browser | Add the player |
|---|---|
| iPad Safari | **Share → Add to Home Screen → Add**. Open **More** if needed and enable **Open as Web App** when offered. |
| iPad Chrome | **Share → Add to Home Screen → Add**. If the option is missing, open the same player URL in Safari and add it there. |
| Android Chrome | **⋮ → Install and create shortcuts → Install**. Some versions label the action **Install app** or **Add to Home screen**. |

Connect to the internet for the first launch and wait for **오프라인 준비 완료** (offline ready). Solo practice then works offline; songs remain local. Session requires internet and sends only files explicitly shared. Direct HTML opening in iPad's Files app is not a supported installation route. Keep the player visible during practice; backgrounding pauses playback. [Detailed steps and official help](web_player/README.md#홈-화면에-앱으로-추가하기)

**From 2.4.8**, the player checks at startup, on returning to the foreground and on reconnecting. You can also press **업데이트 확인** (check for updates). When a new screen is ready, **업데이트 적용 · 다시 열기** (apply update and reopen) appears; the current practice screen never reloads automatically. Finish file loading/audio preparation, stop playback, turn off the metronome, and leave Session before applying. **Select your .magic again after the screen reopens.** An update already downloaded can be applied offline. A failed or offline check does not mean that the app is up to date.

**For the first upgrade from 2.4.7 or earlier**, open the existing home-screen app online so it can receive the update, then refresh or fully close and reopen it. Confirm **3.0.3 or later** in the footer. If you use a browser tab, perform the same steps there. Older screens do not have the new update buttons; reinstalling the home-screen app is not required. [Web update steps (Korean)](docs/UPDATING.md#웹-재생기-업데이트)

**To use the mobile metronome alone, uncheck ‘반주와 함께’ (with accompaniment), then turn it on.** No song file is required. With that box checked, clicks wait until the accompaniment plays; the header shows the waiting state.

## Session mode

The leader can press Ready while uploading. Members without a song can select **리더 파일 받고 준비** to open the shared file and become ready in one step. If the transfer temporarily slows the connection, keep the player open. Readiness returns automatically when the connection stabilizes.

Open the web player from the desktop's **Session 모드 · 함께 연습하기** button, or directly on PC/iPad/Android. The leader opens a `.magic`, enables leader mode and shares the eight-character invite code or QR. Once the leader shares the all-part GP bundle, members can use **리더 파일 받기** in the song panel as soon as they join, before anyone presses Ready. Members can also open their own file. Each player selects a part and presses **합주 준비** (Ready). Cancelling Ready stops playback. Only the person who cancelled needs to press Ready again. When everyone is ready, the leader can resume playback.

The interface changes to a leader control view or a member score view. Only the leader can play, pause and seek; commands are scheduled 1–5 seconds ahead using the relay clock. The leader changes speed/key/count/loop settings while paused. Each participant controls their score part, zoom, volume and metronome on/off. Leaving Session restores the solo interface.

Own files must derive from the identical original recording bytes and duration, though scores may differ. All analysis stays on the desktop and playback runs on each device. The relay handles room membership, clock/control messages and opt-in file transfer. Shared files expire when the room closes, after four hours or on server restart. Solo playback remains offline; Session needs internet and a visible foreground page. Device/Bluetooth output delay is not automatically calibrated. PC leadership uses the same web player as tablets; the native desktop player is paused when opening it.

**GP numbers appearing as black boxes on Windows:** update to v2.1.1 or later, reopen the GP file and run score pre-analysis. GP digits and labels are now stored as vector outlines. Export a new `.magic` to replace previously rendered pages. Original media, GP files and earlier reviewed timeline files are retained.

## Audio choices and desktop import

Check vocals, guitar, drums, bass or keyboards **alone**, the mix **without that instrument**, or the original recording. Piano and synthesizer outputs are also offered separately. Checking both the vocal-free and guitar-free choices prepares both individual versions and one actual vocal-and-guitar-free backing track, selected initially. **전체 선택** checks all 15 base choices and also prepares their complete removal combination; **선택 해제** clears them. Each new combination removes targets from successive residuals, so it can require extra model runs. Verified completed choices and combinations are reused.

To reopen a previously prepared recording, select it and press **저장된 음원 불러오기**. This verifies saved audio without running a model. **재생 음원** lists only currently available cached entries for the working song, or choices actually stored in the open `.magic`. Checking a preparation option does not add it to this playback list. When the current choice is the only available item, its name remains visible with selection disabled. If that cache disappears but another item remains, you can select the remaining audio. Switching pauses playback and retains original-media position, speed, applied key and measure-range settings; press Play explicitly to continue.

On desktop, open **상세 옵션** (details) directly below **처리 내역 보기** (processing history) to see the overall cache folder and current audio/score-analysis paths in a separate dialog. Use **복사** (copy) or **폴더 열기** (open folder) beside a path. The main practice view shows the preparation status without long cache paths.

On desktop and web, **옵션 숨기기** collapses the left options sidebar to make room for the score. Playback and audio choice controls remain available. **옵션 펼치기** restores the sidebar; each app remembers its own preference.

Automatic recommendation uses **Leap Xe for vocals**, **SW six-stem for guitar/drums/bass/piano** and **MVSep Mega for keyboards/synthesizers**. Keyboards cover piano, organ and related key instruments; synthesizers are a separate experimental category that may include synth bass, pads and electronic percussion. These overlapping outputs are listening alternatives and must not be summed as disjoint stems. HyperACE and deux remain selectable. To reuse an older vocal cache, choose its original model and memory setting. A new model requires its own first calculation. [Selection evidence, conditions and model terms](docs/SEPARATION_MODELS.md). Application GPL terms do not grant model-weight permissions.

Use **.magic 연습 파일 열기** on desktop, or place the file in `targets`. Desktop validates and caches the actual archive, then displays its stored pages/timings without recognition or inference. Choose included audio and GP parts independently. Applied export pitch is already baked in; **추가 키** starts at zero. Imported score reanalysis, timing edits and creating a new `.magic` require returning to the original project. The imported file can be transferred unchanged; current audio can be exported as WAV/FLAC.

Exports store shared scores/timing once and each checked audio option separately, with MP3 256 kbps as the default. Files with a combined removal use format 5; files with only base choices keep format 4. The web player decodes the active option on demand. Switching pauses at the same position. In Session each participant may choose a different mix of the same original recording; changing audio cancels that participant's readiness and requires Ready again.

## Rights and attribution

**Recordings and scores are not supplied.** Obtain your own materials lawfully, paying applicable charges. Buying a recording or score does not grant permission to redistribute its original, separated instrumental or `.magic` bundle.

Original project code/docs: GPL-3.0-only. Models and dependencies retain their own licenses. PDF note/symbol recognition uses Audiveris; the PDF-only measure/BPM mode does not run it. GP parsing uses PyGuitarPro; engraving uses Verovio; mobile rate/pitch processing uses Signalsmith Stretch. See [third-party notices and model provenance](THIRD_PARTY_NOTICES.md). Screenshots contain only project-authored synthetic music.

Automated checks distinguish desktop installation, browser execution, and real hardware validation; see [developer documentation](CONTRIBUTING.md). No claim of universally best separation, perfect recognition, or testing on every physical device is made.

Desktop and web footers include a clickable bug and improvement report address. Opening the link composes an email; no files are attached automatically.
