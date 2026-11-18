# AI Coder handoff — Magic-Instrumental

Public release: **1.3.0**. Read `README.md`, `AGENTS.md`, and `CONTRIBUTING.md` before changes. This document intentionally contains no user recordings, score contents, personal paths, or private verification data.

## Product requirements

Python desktop GUI for macOS Apple Silicon and Windows x64. Separate vocals locally, play only the instrumental, preserve pitch while changing speed, and reuse full-content SHA-256 caches. Keep originals under `targets/`, matching PDFs/MusicXML under `score_targets/`, results/settings under `processed/`, and downloaded weights under `models/`.

The right PDF panel is resizable. Repeat/navigation recognition, audio alignment, and zoom-specific camera paths are computed **before playback**. Support score zoom up to 600%, reviewed timeline corrections, and adjustable N-measure previous/next seeks in unfolded performance order. The slim top metronome supports independent playback or original BPM × audio rate, beat circles, meter, and first-beat origin.

One-click installers must bootstrap Python without a preinstalled interpreter. No global project packages or in-project venvs. macOS retains `~/.vocal-separator-and-speed-adj/.venv`; Windows reads the single path in ignored `venv-location.windows.txt`, generated from its public example. Never delete user media/caches to prepare a release.

## Entry points and installation

- `install.command` / `install.bat`: pinned uv archive + SHA-256, managed Python, then `bootstrap.py --setup-only`; includes private Audiveris/Java/English OCR installation and a real synthetic recognition probe.
- `start.command` / `start.bat`: first-install fallback when the external environment is absent; otherwise `bootstrap.py --launch-only` without pip/network installation.
- `installers/runtime.json`: single source of Python/uv pins and download checksums. Official uv downloads its own managed CPython distribution. No PATH, shell profile, or Windows registry changes (`--no-bin --no-registry`).
- `installers/omr.json`: Audiveris 5.11.0 DMG/console MSI, tessdata revision `ced78752cc61322fb554c280d13360b35b8684e4`, English file and license SHA-256 pins, upstream source URLs. All native downloads go directly to official upstream servers; never include them in the public source bundle.
- `bootstrap.py`: standard library only, resolve/validate external environment before packages; OS lock and CUDA selection; successful dependency signature marker; persistent explicit CPU/CUDA profile; FFmpeg alias inside venv. Does not overwrite a nonempty non-venv folder.
- `main.py`: rejects global and project-internal environments before CLI work. CLI imports heavy dependencies lazily.
- `tools/build_release.py`: allowlisted source-only archive. Keep exclusions synchronized with `.gitignore`. Never publish the user's `venv-location.windows.txt`.

## Modules

| Module | Responsibility |
|---|---|
| `config.py` | paths, model commits/checksums/stem names, separation signature, pipeline version |
| `models.py` | locked downloads, SHA-256 verification, atomic replacement |
| `engine.py` | CUDA/MPS/CPU selection and pymss inference |
| `compat.py` | narrowly scoped MPS Mel-band memory workaround |
| `audio.py` | FFmpeg discovery, decoding, header validation, pitch-preserving atempo export |
| `cache.py`, `pipeline.py` | content hashing, snapshots/locks, output validation and cache publishing |
| `cli.py` | gui, doctor, separate, export, score-analyze, version, structured worker events |
| `gui.py` | Qt main window, QMediaPlayer, QProcess workers, split panel, measure navigation |
| `metronome.py` | synthesized click WAVs, independent/media clocks, per-song preferences |
| `score.py` | content-paired scores, atomic JSON, validated timeline, measure seeking |
| `score_analysis.py` | Audiveris process, safe MusicXML, repeat expansion, precomputed audio alignment |
| `omr_runtime.py` | standard-library managed-runtime discovery, signature, cheap launch readiness, dedicated tessdata environment |
| `omr_install.py` | verified downloads, native extraction, full installed-file inventory, synthetic English OCR probe, transactional publication |
| `msi_extract.py` | Windows read-only MSI database / embedded CAB extraction, no installation actions or registry registration |
| `score_geometry.py` | worker-only PDF vector/raster staves, actual barlines, paper sizes, mild deskew |
| `score_symbols.py` | positioned PDF navigation text and repeat-dot supplements, audited in memory |
| `score_layout.py` | PDF-first / validated XML-assisted layout and zoom/viewport camera planning |
| `score_view.py` | standard-library view geometry validation and cached-path interpolation |
| `score_gui.py` | PDFium viewer, analysis/settings editor, lightweight playback following |

## Separation invariants

Cache: `processed/<full audio SHA-256>/<model>-<signature prefix>/instrumental.wav` plus `manifest.json`. Manifest may contain original filenames; it is private runtime data. Signature includes pipeline version, fixed model revision/files/checksums, engine version, sample rate, chunk/batch/TTA/normalization settings. **Device is excluded** so verified results can be shared between CPU/MPS/CUDA.

Cache hit validates full source bytes plus output size/hash/audio metadata. Do not optimize into mtime/title matching. Cache hit must not require model imports, downloads, or an available GPU. Cache miss copies an input snapshot and verifies its hash to detect concurrent source changes. Source/profiles and model downloads have separate file locks.

Validate finite stereo samples, sample rate and duration (within 0.1 s) before publishing. New cache directories publish by rename. Repair replaces only `instrumental.wav` and `manifest.json`, manifest last, preserving other user exports in the folder. Interrupted scratch output is never a completed cache. `PIPELINE_VERSION` is currently **2**; bump it for semantic separation changes.

The default HyperACE v2 vocals secondary stem is exactly **`instrument`**, synthesized from original minus vocals. The alternative deux stems are **`Vocals` / `Instrumental`**. Use the pinned config's exact spelling. The code GPL does not license model weights; see `THIRD_PARTY_NOTICES.md`. Do not describe an unspecified model license as permission for commercial use or redistribution.

## MPS workaround

pymss-core 0.1.16 Mel-band `scatter_add_equivalent` creates a large dense one-hot batched matmul that can exceed MTLBuffer capacity. `compat.mps_mel_band_fix()` temporarily replaces the model module reference with real-valued `index_add_` band summation **only for MPS**. It restores the reference at context exit and never changes installed packages. CPU/CUDA paths stay upstream. Recheck this narrow internal API before updating pymss-core.

## Playback and workers

GUI main thread never loads inference models. QProcess stdout recognizes `@@PRACTICE@@` JSON records using a byte buffer so split UTF-8 sequences are retained. Ordinary output remains diagnostic text; stderr is bounded. Cancellation/close terminates workers and child Java processes (process groups on macOS, taskkill tree on Windows).

Qt FFmpeg QMediaPlayer provides interactive speed via `setPlaybackRate` and pitch compensation via `setPitchCompensation`, enabled when supported. Qt 6.12 credits Signalsmith Stretch for the default FFmpeg backend. Exports always preserve pitch with FFmpeg WSOLA-based `atempo`; slow rates chain supported factors. Do not imply identical live/export waveforms or semitone transposition support. Playback positions, score times, seeks and metronome origin are always **original media seconds**. Do not multiply a seek target by playback rate. Camera/metronome interpolated clocks apply rate only to elapsed wall time since a media anchor.

## Score invariants

### Native OMR setup

Managed tools live outside the project at macOS `~/.vocal-separator-and-speed-adj/tools/` or Windows `%LOCALAPPDATA%\Magic-Instrumental\tools\`, independently of a custom Windows venv path. `omr-<manifest/revision signature>` contains the unchanged app/JRE, `tessdata/eng.traineddata`, `licenses/`, `sources.json` and a success-only `verified.json` file inventory. `INSTALLER_REVISION=1`; consider bumping when extraction/probe semantics change. This signature is separate from reviewed score timelines and separation caches; do not invalidate those merely for an installer update.

Bootstrap invokes `python -m practice_player.omr_install` using the external venv after Python dependencies. Even if pip requirements are unchanged, running install verifies the native file inventory and exercises the runtime. Launch-only checks readiness without network, OMR or hashing the whole app; absent setup instructs users to rerun the install file. Native download/cache/installation is protected by a file lock. Downloads have SHA-256, HTTPS, time and size limits. Never execute unverified bytes. Stage and verify before replacing a managed version; preserve the prior installation on failure and leave manual/system Audiveris untouched.

macOS mounts the official DMG read-only and copies the unchanged `.app`, retaining symlinks/JRE legal files. The official disk image has an AGPL display/continue prompt; automation supplies the affirmative continuation while saving the complete upstream license locally and documenting this behavior. No Gatekeeper disable or global security edits. Windows reads MSI tables through `msi.dll`, expands embedded CABs with built-in `expand.exe`, and reconstructs only the Audiveris subtree. Do not replace this with an elevated MSI installer or system Java. Native extraction tests cover short/long filenames, directory trees, traversal and duplicate rejection; actual extraction is exercised by Windows CI.

The installation probe generates an original PNG with three five-line staves and the English title `MAGIC PRACTICE` using Pillow's bundled font. It calls the native launcher `-version`, then `-batch -step TEXTS -save` with English explicitly selected and private `TESSDATA_PREFIX`. Require both recognized words in the generated `.omr` XML and completion of TEXTS, not only exit code or presence of a traineddata filename. Timeouts terminate child Java processes. Logs remain private in `tools/install-omr.log`; temporary probe media are removed. This is a native/English OCR readiness test, not proof of arbitrary score transcription accuracy.

`AUDIVERIS_EXECUTABLE` explicitly overrides discovery; otherwise prefer the verified managed launcher, then PATH/legacy manual locations. Managed Audiveris always uses its own verified tessdata despite unrelated inherited `TESSDATA_PREFIX` and passes the English default-language constant for both the probe and real OMR, insulating it from other installations' saved language preferences. Explicit/manual programs retain their custom data environment and language preferences. Never install or perform this probe from GUI playback callbacks.

Scores are paired by normalized stem for convenience, then cached by file content hashes and analysis settings. PDF/MusicXML/audio are rechecked around analysis. Use defusedxml, bounded MXL reads (32 MB), no archive extraction, PDF page limits, bounded measure/route counts, and an Audiveris timeout. English OCR data absence is an error because textual navigation could be missed.

`music21` parses notes, meter and expressions. Custom `expand_navigation()` also reads `<sound>` navigation marks: generic repeat expansion mishandles some long first endings with D.S. Preserve first/second ending and post-D.C./D.S. repeat behavior tests. Ambiguous multiple jumps require a reviewed explicit route rather than silently invented order.

Audio alignment is offline: 11,025 Hz mono, chroma/cosine cost, full-sequence DTW, bounded ~3,000×3,000 matrix and slope constraints. Trim low-energy margins. This is not realtime score following or a partial-score subsequence mode. Similarity is a feature diagnostic, not an accuracy probability. OMR and time alignment are estimates until reviewed; do not claim automatic correctness.

Page-only timeline version remains **1**; optional `scroll.version=1` adds camera paths. The layout/recognition engine is **2**, independently included in both timing and view cache signatures. `score_geometry.py` detects PDF staves and barlines independently of XML `<print>` and width metadata, then requires the full original measure count and fixed simultaneous 5/6-line staff pattern to agree. Vector endpoints plus raster visibility reject note stems; nested form matrices, PDF rotation and crop offsets are handled through PDFium coordinates. Raster-only pages use horizontal morphology, cap rejection and bounded small-skew search. Preserve page dimensions, normalized actual bar boxes, layout method and diagnostics in `pdf_layout`. This is bar-level geometry, not exact note-level or tablature pitch recognition.

If PDF geometry fails, `score_layout.py` can use the earlier XML print/width method only after matching page/staff/system structure, with an explicit warning. If both fail, timing analysis may retain XML page estimates with a warning, but camera planning fails; do not invent equal-width bars. Changing/hidden staves and multi-measure rests need matching print metadata or review.

`score_symbols.py` reads character boxes, not PDF text-stream newlines; only spatially assigned navigation labels and two isolated repeat dots are eligible. It safely parses and supplements XML in memory, never rewriting the user's files. Existing marks take precedence; conflicting DC/DS commands fail. Record additions in `pdf_symbols_added`. Ambiguous destinations, multiple jumps and unsupported notation still require review. Graphical segno/coda without Unicode/text, volta brackets and pitches remain the OMR/XML engine's responsibility. Explicit user routes bypass supplementation; view-only analysis never changes navigation or reviewed cue times/pages.

Each cue's camera frame contains page/source_index/system and position/transition data. Cache key includes base timeline content (excluding old view fields), scroll version, layout engine version, zoom and viewport. Existing reviewed times/offset/flags survive view-only analysis. Never perform OMR, feature extraction or staff detection from playback callbacks.

PDFium renders at the chosen zoom with bounded source image size and a 3-page pixmap cache. A 16 ms timer interpolates precomputed camera frames from media time; pause/stop freezes it. Zoom/viewport mismatch disables automatic movement and asks for reanalysis. Manual wheel/scroll/page changes disable following. Timeline page edits invalidate old scroll geometry. Persistent window/splitter sizes improve cache reuse.

Measure seeking uses `ScoreTimeline.playable_measures(duration)` and `measure_target(media_seconds, delta, duration)`. Add ±N to **visit index**, not printed bar number. Handle intro/outro, partial first cue, offset and track bounds. Seek to `ceil(seconds * 1000)` to avoid landing immediately before a fractional boundary. Keep play/pause state unchanged. The score panel's `timeline_changed` signal updates button availability.

## Metronome

Synthesized 30 ms clicks via QSoundEffect; dedicated 0–100% click volume (default 35%) and mute, independent of QAudioOutput music gain. `MetronomeBar` accepts the window QSettings through `preferences=`; persist integer percent under `metronome/volume` in `processed/ui.ini`, globally across tracks, including standalone use. Missing/invalid saved values fall back to 35%. Mute applies zero effective gain while retaining the selected value. Zero gain stops pending clicks and skips new audio dispatch; visual beats continue. Volume/mute changes must never reset the scheduler or alter the music output. Meter numerator 1–12, denominator 2/4/8/16; quarter-note BPM 20–400. Independent monotonic clock or media anchor + elapsed × rate; 5 ms precise timer detects boundaries rather than accumulating tick counts. Late ticks/seek jumps must not burst queued clicks. Pause/stop silence the linked clock. Fixed entered tempo only; no automatic tempo/meter change tracking or sample-accurate audio synchronization.

Checking `linked` arms the click toggle without starting audio. Users can explicitly stop clicks while remaining linked. `ClickScheduler` tracks the highest handled beat within a transport segment: ordinary position corrections, pause/resume and rate changes retain it, preventing duplicate clicks. Initial/mid-beat starts wait for a boundary (5 ms initial tolerance); late ticks only click within min(35 ms, 35% of the wall-clock beat interval). Never catch up with a burst.

All application seeks must go through `Window.seek_to()`, which calls `begin_seek()` / `end_seek(actual_position)` around QMediaPlayer.setPosition. This resets the click segment even for small seeks within the same beat and preserves playback state. Implicit loops/external large jumps are detected from transport samples; small backwards clock corrections retain the watermark. Pause/resume reanchors without extrapolating paused time. Loading/end/error reset segments; a buffer stall suspends clicking without changing phase. Independent mode ignores audio seeks/stalls. `align_now` uses interpolated original media time while playing, exact position while paused. Track binding resets the scheduler even if the new settings equal the old ones.

Audiveris is a separately installed Java program invoked by CLI, not a bundled Python library. The one-click installer now prepares it automatically. Its source headers permit **AGPL-3.0-or-later**; English tessdata is **Apache-2.0**. Preserve the README attribution, pinned upstream license/source links in `THIRD_PARTY_NOTICES.md`, and original notices/Java legal files in the installation. Automated installation is not itself forbidden, but do not describe separate processes or download-only source releases as a blanket exemption for future binary redistribution, modifications or network services.

Per-song BPM/meter/origin live under the audio hash; global window/measure-step settings live in `processed/ui.ini`. These files are never public source. Do not auto-start clicks at app launch.

## Validation and releases

Use the external environment commands in `CONTRIBUTING.md`. Tests cover cache corruption/repair, FFmpeg pitch/duration, GUI cancellation/playback, repeat/coda routes, synthetic DTW, PDF navigation, camera geometry/cache, metronome clocks, and measure boundary seeking. Geometry tests include metadata-free mixed/landscape pages, nested forms/crops/rotation, faint skewed raster scans, 5/6-line multi-staff systems, unequal bar widths, short final systems, navigation supplementation and conflict rejection. Fixtures are entirely synthetic. Real-weight tests are opt-in (four model/memory combinations); they exercise inference, not subjective quality.

Apple MPS real-model inference was exercised during development. Windows CPU/CUDA dependencies were resolved for x64. Both platform installers now exercise the actual Audiveris Java launcher, native OCR libraries and English recognition in CI. Windows physical audio/NVIDIA hardware and desktop security prompts still need validation; do not erase that distinction after a CI pass. Native MSI extraction uses no administrator-only API or installer actions; enterprise application-control policies may still block execution.

Before public upload: run checks, build allowlisted ZIP, inspect every included path and scan content for private media names, personal home paths, emails and credentials. Never publish logs/manifests/cache checksums from a user's song. Update version + CHANGELOG, user docs and this handoff. License is GPL-3.0-only for original project code/docs; weights/dependencies keep upstream licenses. Users must supply lawfully obtained audio and scores. Public examples must use synthetic data or permitted media.
