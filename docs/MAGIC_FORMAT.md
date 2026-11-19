# `.magic` format v1 and web player engineering

This is developer documentation. User instructions are in the desktop README and `web_player/README.md`.

## Archive contract

A `.magic` file is a seekable **ZIP_STORED** archive: no ZIP-level compression, encryption, split disks, ZIP64 or data descriptors. PNG assets use their usual internal PNG encoding; audio is PCM16 stereo 44.1 kHz WAV. Export writes a private temporary archive beside the destination, rechecks all inputs, then publishes it atomically. Existing exports require explicit overwrite selection. The original audio, PDF, GP and reviewed timeline are never edited.

| Entry | Meaning |
|---|---|
| `manifest.json` | `format: Magic-Instrumental`, `version: 1`, title, audio, score, page list, practice defaults, per-entry byte counts and SHA-256 |
| `audio/instrumental.wav` | Current desktop key, original duration; speed is **not** baked into this file |
| `score/source.pdf` or `score/source.gp3/gp4/gp5` | Unmodified source score |
| `pages/0001.png` … | Prepared display pages, long edge 2200 px, including the selected GP part |
| `timeline.json` | Performed cues, original-media start/end seconds, one offset, reviewed flag, warnings and normalized bar boxes |

For native GP, `score.kind` is `gp`, with selected `track` and `part`. All pages depict that part. Mobile never parses GP or invokes OMR. Choosing a different part requires a desktop re-export. A PDF paired with a GP notation source remains a PDF display package. No absolute filesystem paths, cache folder names or full desktop manifests are serialized. Naturally, user-supplied score content and its own metadata remain in the private archive; `.magic` is **not** a public/redacted format.

`audio.pitch_semitones` records the baked desktop shift, ±12. Mobile starts with extra shift 0 and can add ±12; the display reports their sum relative to the original. No printed-note/TAB transposition occurs. `practice` contains quarter-note BPM, meter numerator/denominator and the original-media first-beat origin. Scores may contain repeats and several visits with the same printed number. Loop endpoints refer to performed cue indexes, with inclusive final measure and exclusive media end.

## Reader limits and trust

The browser accepts at most 1 GiB total, 512 MiB per audio entry, 200 pages, 20,000 unfolded cues and 205 archive members. It checks central/local header agreement, offsets, non-overlap, CRC, uncompressed sizes, a fixed ASCII path allowlist, duplicates and exact member inventory. Manifest/timeline JSON have separate small bounds. It checks cue ordering/pages/geometry, PCM duration/channel metadata, audio SHA-256, PNG signatures and image dimensions. All labels use `textContent`, images are PNG Blob URLs, and no package-supplied code runs. It never extracts files to disk. Memory-limited devices can fail below these limits; an AudioBuffer needs more memory than the WAV.

Source files are preserved in the package for portability but are not executed or parsed by mobile. They have ZIP CRC and manifest SHA-256 metadata; only assets actually opened by the reader undergo content verification at playback load. A checksum detects corruption, not an authenticated publisher.

## Original time, audio scheduling and count-in

Signalsmith Stretch 1.3.2's embedded WASM runs in an AudioWorklet. `AudioContext.currentTime` anchors each segment `{input, out, rate, end, until}`. Display position is `input + max(0, currentTime - out) * rate`, clipped to the exclusive end. Input time is always original-media seconds. `inputTime` messages from the upstream worklet include lookahead latency and therefore are not the UI's authoritative media clock.

Schedule both the active segment and its inactive end before playback. During a loop, queue the next segment ahead of the boundary; when count-each is enabled, leave an explicit inactive count interval. The generated vendor adapter fixes two upstream scheduling timestamp issues; see the license notice and exact build assertions. Audio spectrum tests verify actual output, not only the app clock.

Period = `240 / (BPM * denominator * rate)` for linked clicks/counts. For N beats and original target beat `b`, start count at `ceil(b) - N`; emit exactly N beats, then enter the track at the unchanged target. Fractional entries shorten the last interval. The same AudioContext schedules click nodes and the music segment. Standalone clicks use an independent origin and rate 1. Seeking/rate/key/meter changes cancel queued events and reanchor once. Gain changes preserve clock phase. Backgrounding deliberately pauses and cancels all queued sounds; no background-playback promise.

## Responsive score and offline app

Desktop GP rendering is pre-analysis only: source SHA + part + GP converter v2 + render v2 + pinned Verovio 6.4.1. Verovio measure IDs and SVG staff bounds provide native geometry; no OCR or PDF geometry guessing is used for this route. The internal PDF is a display artifact. Alignment uses the single original notation part; the displayed TAB duplicate must not double the alignment notes.

Mobile loads at most three page Blob URLs, renders one current page, and interpolates the saved bar coordinates for the current viewport/zoom. It does not rerun recognition/alignment. Manual gestures suspend following until the user re-enables it. Reviewed page corrections override generated geometry; their unknown positions fall back to page bounds.

`tools/build_web.py` creates `web_player/index.html` with all JS/CSS/WASM in one file, and a versioned service worker. Initial HTTPS (or localhost during development) is required for PWA installation. The worker caches only the static app, never `.magic` contents. Each launch asks the user to select a local file again. iPad Files HTML previews are not presented as a supported installation route. Static hosting needs no server-side code, API, analytics or account.

## Development

Use the external venv. Run `python tools/build_web.py` after changing HTML/CSS/JS/vendor pins. Run `python -m playwright install chromium webkit` and `RUN_WEB_TESTS=1 python -m pytest -q tests/test_web_player.py` for real browser/audio/GP/PDF/offline/corruption checks. `tools/capture_v2_screenshots.py` uses only authored synthetic fixtures. Never put private test bundles into the public allowlist.

The `main` branch maintains all sources. `mobile-player` exposes the static app at the branch root, its beginner README, GPL license, upstream notices/vendor source and the builder under their original paths where needed. Publish only files selected by the release allowlist. GitHub Pages can serve the branch root; enabling it requires repository administration, which the connected code-writing app does not provide.
