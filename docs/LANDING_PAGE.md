# Landing page — developer deployment branch

This branch contains the independent introduction/install page for Magic-Instrumental. General users should open the page itself or the application's main README.

- `index.html`: standalone HTML/CSS; no account, upload, runtime JavaScript or application backend dependency.
- `LICENSE`: GPL-3.0-only for original page code.
- The visible Session link opens the existing GitHub Pages player.
- The footer includes the requested public author/CV attribution and the main repository's third-party notice link.

The integrated source is `landing/index.html` on `main`. Publish a reviewed copy here and deploy this branch to a separate static web root. Operator configuration belongs in developer documentation and private server files, never in the page. See the main branch's [deployment guide](https://github.com/jeongwoo-gobroad/Magic-Instrumental/blob/main/docs/DEPLOYMENT_BRANCHES.md).

Prepare versioned links with the release changes, but publish the page only after both immutable release ZIP assets exist and their downloaded bytes match the release checksums. Verify desktop/phone layout, download response, footer links and supported-format wording. Place installation first, followed by score requirements, first-song practice, Session and the remaining material. Recommend Guitar Pro sources (GP3/4/5) and explain how preparing an instrumental separates the vocals from a song recording.

## Score and following descriptions

For v2.4.2, distinguish notation data from PDF display and geometry. A corresponding selected MusicXML file, or a valid MusicXML attachment embedded in the PDF, is preferred for notation analysis. Without it, PDF note recognition can be supplemented by supported printed navigation text and music-font glyphs. An arbitrary XML attachment is not necessarily MusicXML. Do not promise universal recognition of a publisher's scores, every font, or a scan.

Geometry/BPM timing is a limited fallback when notation is unavailable or recognition fails. It can find measure layouts in paired standard-notation/TAB PDFs and multi-staff scores; this does not split parts or transcribe TAB numerals into pitches. Invalid or conflicting navigation and a failed supplied MusicXML analysis must not be described as successful straight-through playback. Explain that users need to check repeat/coda routes, pickups, meter/tempo changes and the actual recording before relying on following. PNG/JPG/JPEG are not direct inputs, and image-based PDFs may fail recognition.

Describe the PDF MusicXML selector and audio/fixed-tempo timing choices using the shipped UI labels. The reader displays the original PDF; tall multi-staff systems need an appropriate user-selected zoom or page fit. Once analysis has completed, current-measure tracking and enabled page following continue after window, zoom or preparation-panel changes. A changed viewport can require view-only reanalysis for movement within the page. Use the actual action: `악보 사전 분석` → `기존 마디 시간표 유지 · 화면 이동 경로만 분석`. It preserves the existing timeline. Corrected timelines require a new `.magic` export for the tablet.

For v2.4.4, describe `마디 강조` as an optional display setting, on by default on both desktop and web. Each app/browser remembers its own choice. Turning it off hides only the colored measure overlay; following, measure numbers, playback and timing remain independent. Turning it back on uses the existing measure positions. Keep the landing explanation brief and leave the detailed controls in the user guides.

## Deployment verification

A successful push to `landing-page` is not a server deployment. Compare the public HTTPS response bytes with the reviewed branch's `index.html` after updating the static root. Record branch publication and live-host verification separately. Keep the Session links, author/CV attribution, third-party notice and existing design intact.

Operator-only LAN packages may transport the reviewed HTML to the existing host. Before replacement, validate the release and packaged payload, identify the currently served file by a reviewed SHA-256, and refuse unrecognized local edits. Replace only the landing file atomically, preserving metadata; verify the served HTTPS bytes with ordinary certificate checks and restore the previous page on failure. A landing-only update must not restart the relay, alter DNS/TLS configuration or publish LAN addresses and operator credentials in the public page or repository.
