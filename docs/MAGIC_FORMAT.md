# `.magic` format v1/v2/v3 and web player engineering

This is developer documentation. User instructions are in the desktop README and `web_player/README.md`.

## Archive contract

A `.magic` file is a seekable **ZIP_STORED** archive: no ZIP-level compression, encryption, split disks, ZIP64 or data descriptors. PNG assets use their usual internal PNG encoding. Since application v2.4.5, new exports default to 256 kbps MP3; users can choose 128, 192 or 320 kbps MP3, or PCM16 stereo 44.1 kHz WAV. MP3 compression applies inside the audio entry, while the ZIP container remains stored. Export writes a private temporary archive beside the destination, rechecks all inputs, then publishes it atomically. Existing exports require explicit overwrite selection. The original audio, prepared instrumental, PDF, GP and reviewed timeline are never edited.

| Manifest version | Score contents | Embedded audio |
|---|---|---|
| `1` | PDF, or a legacy selected GP part | `audio/instrumental.wav` |
| `2` | All-part GP | `audio/instrumental.wav` |
| `3` | PDF or all-part GP | `audio/instrumental.mp3` |

Choosing WAV continues to write version 1 for PDF and version 2 for all-part GP. Existing WAV archives remain readable and are not migrated automatically. New MP3 archives require the MP3-capable v2.4.5 player build. An earlier published build used the same display version for the iPad picker fix, so the version label alone does not establish format 3 support. Refresh online or reopen the tab to receive the updated assets. Do not promise a particular update error from an already deployed reader: its path allowlist can reject the MP3 entry before checking the manifest version.

| Entry | Meaning |
|---|---|
| `manifest.json` | `format: Magic-Instrumental`, `version: 1`, `2` or `3`, title, audio, score, page list, practice defaults, per-entry byte counts and SHA-256 |
| `audio/instrumental.wav` or `audio/instrumental.mp3` | Version-specific audio entry at the current desktop key and original duration; speed is **not** baked into this file |
| `score/source.pdf` or `score/source.gp3/gp4/gp5` | Unmodified source score |
| `pages/0001.png` … | Prepared display pages, long edge 2200 px, including the selected GP part |
| `timeline.json` | Performed cues, original-media start/end seconds, one offset, reviewed flag, warnings and normalized bar boxes |

For native GP, `score.kind` is `gp`, with selected `track` and `part`. In v1 all pages depict that part. In v2 and GP v3, `parts` lists every track, name, percussion flag, page list and timeline path. The selected reference part uses the root pages/timeline; other tracks use `parts/NN/pages/0001.png` and `parts/NN/timeline.json`. `timing_basis` declares the selected track and `shared: true`. Every part is parsed and its performed source-index/visit/beat/duration route must match the reviewed reference. Only geometry differs; start/end/offset times are shared. Mobile never parses GP or invokes OMR. Switching parts verifies the common time route and preserves transport. A PDF paired with a GP notation source remains a PDF display package. No absolute filesystem paths, cache folder names or full desktop manifests are serialized. Naturally, user-supplied score content and its own metadata remain in the private archive; `.magic` is **not** a public/redacted format.

`audio.pitch_semitones` records the baked desktop shift, ±12. Mobile starts with extra shift 0 and can add ±12; the display reports their sum relative to the original. No printed-note/TAB transposition occurs. `practice` contains quarter-note BPM, meter numerator/denominator and the original-media first-beat origin. Scores may contain repeats and several visits with the same printed number. Loop endpoints refer to performed cue indexes, with inclusive final measure and exclusive media end.

## MP3 metadata and the original PCM time grid

Format 3 adds an explicit compressed-audio contract. Create the current-key, 44.1 kHz stereo PCM representation first, then encode it as MP3. The canonical frame count is measured before MP3 encoding; a frame contains one sample for each channel. Lossy encoding changes sample values, but must not change the original-media coordinate system used by cues, offsets, seeks, loops, pitch metadata or Session.

| `audio` field | Format 3 contract |
|---|---|
| `path` | `audio/instrumental.mp3` |
| `codec` | `mp3` |
| `bitrate_kbps` | Integer `128`, `192`, `256` or `320`; export default `256` |
| `sample_rate` | `44100` |
| `channels` | `2` |
| `frames` | Positive integer number of canonical PCM frames before compression |
| `duration` | Canonical `frames / 44100` seconds |
| `pitch_semitones` | Existing baked desktop key shift |
| `source_sha256` | Existing original-recording identity, independent of the compressed entry hash |

Format 3 accepts the exporter's bounded MP3 profile: no ID3 tag, MPEG-1 Layer III, 44.1 kHz stereo, fixed 128/192/256/320 kbps, and an Info/Xing header with LAME-style encoder-delay and padding metadata. This is the embedded-audio contract, not a general-purpose MP3 importer. Retain that metadata so a compatible decoder can recover the original PCM length.

Before native decoding, the browser walks the actual MPEG frames. It checks every frame's supported rate/channel/bitrate profile, the Info/Xing file-byte and audio-frame counts, and the canonical PCM length calculated from the stream's frame count minus encoder delay and padding. These values must match the manifest and remain within the PCM16-equivalent 512 MiB bound. A falsely short duration or low declared bitrate cannot bypass that bound. Native decoding then separately requires an exact match for decoded frame count, sample rate and channel count. A failed decode or mismatch stops loading with WAV export/browser-update guidance; do not silently shift cue times or accept an altered media duration. Legacy v1/v2 WAV retains its existing PCM validation and requires no added codec fields.

The desktop opens `연습 파일 반주 음질` from the existing `.magic` export button. `반주 음질` offers the four MP3 bitrates and `WAV · 무손실`; `저장 위치 선택…` continues to the existing file dialog and overwrite confirmation. Persist the confirmed selection in QSettings `magic_export/audio_quality` as `mp3:128`, `mp3:192`, `mp3:256`, `mp3:320` or `wav`, defaulting to `mp3:256`. Cancelling either dialog or rejecting an overwrite leaves the previous preference unchanged.

The private JSON request for `magic-export --request` adds `audio_format` (`mp3` or `wav`, default `mp3`) and `mp3_bitrate_kbps` (integer `128`, `192`, `256` or `320`, default `256`). Existing input, timeline, key, destination and explicit-overwrite fields retain their meanings. WAV requests can omit the MP3 bitrate field. Do not include the request JSON or its private source paths in a public package.

A lower encoded-audio size does not imply the same reduction for the whole archive: source scores and PNG pages also contribute. Nor does it guarantee lower playback memory. The browser decodes MP3 into PCM before playback, and audio buffers and worklet copies require additional memory. Encoding quality affects new exports only; no background conversion of existing `.magic` files occurs.

## Reader limits and trust

The shared local-file input deliberately omits `accept`. iPad file pickers can exclude the custom `.magic` extension when it is mixed with the known `application/zip` MIME filter. Solo and Session's own-file button use this same input. Do not filter by `File.type` or require an extension rename: browsers may supply an empty or generic MIME type, and the archive reader validates the bytes. The picker fix requires no format migration or re-export of existing bundles. Session's downloaded-file path continues to use the same reader without the local picker.

The browser accepts at most 1 GiB total, 512 MiB per stored audio entry, 200 pages per part, 64 parts, 1,000 total pages, 20,000 unfolded cues and 1,100 archive members. MP3 also checks the canonical PCM16-equivalent size against 512 MiB before decoding, using both the declared metadata and the verified MPEG stream length. It checks central/local header agreement, offsets, non-overlap, CRC, uncompressed sizes, a fixed ASCII path allowlist, duplicates and exact member inventory. Manifest/timeline JSON have separate 1 MB/8 MB bounds. Only the active part timeline is retained; all-part GP page and timing paths are an exact inventory in v2/v3. It checks cue ordering/pages/geometry, version-specific audio metadata and decoded duration/channels, audio SHA-256, PNG signatures and image dimensions. All labels use `textContent`, images are PNG Blob URLs, and no package-supplied code runs. It never extracts files to disk. Memory-limited devices can fail below these limits; these input bounds are not a guarantee that decoded audio fits a device's available RAM.

Source files are preserved in the package for portability but are not executed or parsed by mobile. They have ZIP CRC and manifest SHA-256 metadata; only assets actually opened by the reader undergo content verification at playback load. A checksum detects corruption, not an authenticated publisher.

## Original time, audio scheduling and count-in

`practice.origin` accepts −30 through 1800 seconds in the archive, the browser and Session transport. A negative value places the first beat before the recording starts. Preserve its value so the beat phase is the same on the desktop and the tablet.

Signalsmith Stretch 1.3.2's embedded WASM runs in an AudioWorklet. `AudioContext.currentTime` anchors each segment `{input, out, rate, end, until}`. Display position is `input + max(0, currentTime - out) * rate`, clipped to the exclusive end. Input time is always original-media seconds. `inputTime` messages from the upstream worklet include lookahead latency and therefore are not the UI's authoritative media clock.

Schedule both the active segment and its inactive end before playback. During a loop, queue the next segment ahead of the boundary; when count-each is enabled, leave an explicit inactive count interval. The generated vendor adapter fixes two upstream scheduling timestamp issues; see the license notice and exact build assertions. Audio spectrum tests verify actual output, not only the app clock.

Period = `240 / (BPM * denominator * rate)` for linked clicks/counts. For N beats and original target beat `b`, start count at `ceil(b) - N`; emit exactly N beats, then enter the track at the unchanged target. Fractional entries shorten the last interval. The same AudioContext schedules click nodes and the music segment. Standalone clicks use an independent origin and rate 1. Seeking/rate/key/meter changes cancel queued events and reanchor once. Gain changes preserve clock phase. Backgrounding deliberately pauses and cancels all queued sounds; no background-playback promise.

## Responsive score and offline app

Desktop GP rendering is pre-analysis only: source SHA + part + GP converter v2 + render v2 + pinned Verovio 6.4.1. Verovio measure IDs and SVG staff bounds provide native geometry; no OCR or PDF geometry guessing is used for this route. The internal PDF is a display artifact. Alignment uses the single original notation part; the displayed TAB duplicate must not double the alignment notes.

Mobile loads at most three page Blob URLs, renders one current page, and interpolates the saved bar coordinates for the current viewport/zoom. It does not rerun recognition/alignment. Manual gestures suspend following until the user re-enables it. Reviewed page corrections override generated geometry; their unknown positions fall back to page bounds.

PDF-only measure/BPM exports use format 3 with MP3 or format 1 with WAV, require no XML source, and retain the original full score without part separation. Their portable timeline has optional `camera_mode: "system"`: horizontal following still uses the current bar, while vertical position stays at the common system top instead of sweeping through simultaneous instruments within every bar. Readers validate this optional enum; missing fields keep legacy behavior. The timing is fixed BPM/meter or user-reviewed timing, not automatic audio alignment or repeat recognition.

`tools/build_web.py` creates `web_player/index.html` with all JS/CSS/WASM in one file, and a versioned service worker. Initial HTTPS (or localhost during development) is required for PWA installation. The worker caches only the static app, never `.magic` contents. Each launch asks the user to select a local file again. iPad Files HTML previews are not presented as a supported installation route. Solo static hosting needs no server-side code, API, analytics or account. Optional Session mode uses the separately deployed relay described in `SESSION_SERVER.md`; its API responses are never persisted in the app cache.

## Development

Use the external venv. Run `python tools/build_web.py` after changing HTML/CSS/JS/vendor pins. Run `python -m playwright install chromium webkit` and `RUN_WEB_TESTS=1 python -m pytest -q tests/test_web_player.py` for real browser/audio/GP/PDF/offline/corruption checks. `tools/capture_v2_screenshots.py` uses only authored synthetic fixtures. Never put private test bundles into the public allowlist.

Desktop Chromium/WebKit automation can check the picker buttons and load files with an empty MIME type, but injected files bypass the native iPad file chooser's filtering. Report physical iPad selection separately from browser parsing and playback checks. Regenerate `index.html` and `sw.js` together, then synchronize the deployment branch's static-root copies and their `web_player/` counterparts. The worker revision and integrity must match the new HTML; an already open page may need an online refresh or tab reopen to show it.

The `main` branch maintains all sources. `mobile-player` exposes the static app at the branch root, its beginner README, GPL license, upstream notices/vendor source and the builder under their original paths where needed. Publish only files selected by the release allowlist. GitHub Pages can serve the branch root; enabling it requires repository administration, which the connected code-writing app does not provide.


## Session scheduling

`ensemble.js` measures WebSocket round trips against the relay's monotonic UTC mapping. `performance.now()` and the lowest-RTT sample estimate the server offset; device wall-clock values are not trusted. The relay chooses a 1–5 second future deadline from the slowest participant's measured RTT and requires acknowledgements from every ready participant. Missing acknowledgements cancel the reservation before the deadline. Sequence numbers invalidate stale commands.

The browser maps the deadline into its running AudioContext. It schedules an inactive boundary and optional new counted segment in advance; UI state changes at the deadline. Existing loop scheduling invalidates/rearms that pending boundary rather than overwriting it. Old metronome events are clipped at the deadline; new count events share the same output clock. Member score switching never changes the shared media clock. Speed/key/meter/loop editing is restricted to a paused leader to avoid competing live clock edits.

Original recording SHA-256 and duration bind room readiness. `audio.source_sha256` was added to new exports; v1 fallback requires identical PCM entry SHA. The absolute original-song key is shared; each device subtracts its own baked key before applying the extra web shift. Different score routes are permitted for separately authored member files, but their reviewed times must use the same original recording coordinates. Room loop endpoints are absolute media seconds, not member cue indexes.

The server enforces leader-only transport independently of the UI. Background/audio suspension, readiness loss, connection loss and room expiry cancel scheduled playback. No audio is streamed between participants. Physical output/Bluetooth latency and long-term audio-clock drift are not calibrated; browser-state timing tests are not measurements of acoustic inter-device synchronization.
