# Third-party notices and model terms

Original Magic-Instrumental code and documentation: **GNU GPL v3.0 only (SPDX: GPL-3.0-only)**. Copyright © 2026 Magic-Instrumental contributors. See [LICENSE](LICENSE). Third-party software and model weights are not relicensed by this project. No model weights, recordings, scores, or native third-party applications are included in the source release.

## Model weights

The app downloads author-published weights/configuration on first use. `practice_player/config.py` is the exact revision/checksum source of truth. Check the author's terms before use; a downloadable checkpoint is not automatically an open-source license or permission for every purpose.

| Model / author | Pinned revision | Terms checked 2026-11-18 |
|---|---|---|
| [BS-Roformer-HyperACE / pcunwa](https://huggingface.co/pcunwa/BS-Roformer-HyperACE) | `5b1f8283125d5e4a3614d0e3635a636e09c84059` | The checked model card does not specify a weight license. Obtain clarification from the author for your use; commercial use and redistribution permission are not established by this repository. |
| [mel-band-roformer-deux / becruily](https://huggingface.co/becruily/mel-band-roformer-deux) | `402dd9a19baaed8c6f1b54458ce6ae2ae19f354e` | The author declares [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/): noncommercial terms and attribution requirements apply. |

HyperACE uses `v2_voc/bs_roformer_voc_hyperacev2.ckpt` and its matching config; deux uses `becruily_deux.ckpt`. These are unmodified downloaded weights. The app verifies hashes and uses a runtime MPS inference compatibility fix without rewriting checkpoints. Selection was informed by author cards and [MVSEP BS-RoFormer](https://www.mvsep.com/algorithms/34) / [Mel-Band](https://mvsep.com/algorithms/49) comparisons, not an exhaustive independent benchmark. Do not describe all bundled model choices as commercially licensed.

## Software and runtime

Dependencies are installed from their distributors, not copied into this source repository. The following summarizes principal projects; the actual installed distributions' license files and bundled dependency notices remain authoritative. Transitive packages are pinned in the platform lock files and retain their own terms.

| Component | Purpose | Upstream license / notice |
|---|---|---|
| [pymss](https://github.com/pymss-project/pymss), [pymss-core](https://github.com/pymss-project/pymss-core) | Model inference | MIT; preserve upstream and vendored-component notices |
| [PyTorch](https://github.com/pytorch/pytorch/blob/main/LICENSE) | CPU/CUDA/MPS tensor runtime | BSD-style license and bundled third-party terms; NVIDIA components retain their own terms |
| [Qt for Python / PySide6](https://doc.qt.io/qtforpython-6/licenses.html) | Desktop GUI and playback | LGPLv3/GPLv3 and applicable Qt module/third-party terms; commercial alternative is available upstream |
| [Signalsmith Stretch](https://doc.qt.io/qt-6/qtmultimedia-attribution-signalsmith-stretch.html) | Qt FFmpeg backend pitch compensation | MIT; Copyright (c) 2022 Geraint Luff / Signalsmith Audio Ltd. Distributed inside Qt, not vendored by this project |
| [pypdfium2](https://github.com/pypdfium2-team/pypdfium2) | PDF rendering | BSD-3-Clause / Apache-2.0 plus PDFium and bundled third-party licenses |
| [pypdf](https://github.com/py-pdf/pypdf) | PDF structure | BSD-3-Clause |
| [PyGuitarPro 0.11](https://github.com/Perlence/PyGuitarPro) | Direct GP3/4/5 notation reading | [LGPL-3.0-only](https://github.com/Perlence/PyGuitarPro/blob/master/LICENSE); separately installed Python library, not vendored |
| [attrs](https://github.com/python-attrs/attrs/blob/main/LICENSE) | PyGuitarPro data models | MIT |
| [music21](https://github.com/cuthbertLab/music21) | MusicXML and notation | BSD-3-Clause; this app does not distribute its example music corpus |
| [librosa](https://github.com/librosa/librosa) | Offline audio alignment | ISC |
| [SciPy](https://github.com/scipy/scipy/blob/main/LICENSE.txt) | Offline score geometry and image morphology | BSD-3-Clause and bundled notices |
| [NumPy](https://github.com/numpy/numpy) | Numeric processing | BSD-3-Clause and bundled notices |
| [SoundFile](https://github.com/bastibe/python-soundfile) | Audio files | BSD-3-Clause; bundled libsndfile has its own LGPL terms |
| [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg) | FFmpeg executable distribution | BSD-2-Clause wrapper; **FFmpeg binary licensing is separate** |
| [FFmpeg](https://ffmpeg.org/legal.html) | Decode and tempo export | LGPL/GPL and optional codec terms depend on the actual build. Inspect `ffmpeg -L` and its distributor's source information before redistributing binaries. |
| [filelock](https://github.com/tox-dev/filelock) | Cache locks | Unlicense |
| [defusedxml](https://github.com/tiran/defusedxml) | XML safety | PSF-2.0 |
| [uv](https://github.com/astral-sh/uv) | Bootstrap only | MIT OR Apache-2.0; downloaded as a checksum-pinned executable |
| [CPython](https://docs.python.org/3/license.html) / [python-build-standalone](https://github.com/astral-sh/python-build-standalone) | Private Python runtime | Python license and distribution dependency notices; uv downloads this runtime separately |
| [Pillow](https://github.com/python-pillow/Pillow/blob/main/LICENSE) | Synthetic installation-test image and private OMR input preparation | MIT-CMU and bundled dependency/font notices |
| [Audiveris](https://github.com/Audiveris/audiveris) | PDF music recognition | AGPL-3.0-or-later; official standalone distribution downloaded by the installer |
| [Tesseract OCR data](https://github.com/tesseract-ocr/tessdata) | English OCR | Apache-2.0; pinned official eng data downloaded by the installer |

A binary repackager must review and satisfy the licenses of the actual dependencies shipped, including source/notice obligations. This source-only release does not grant rights to redistribute third-party model weights or user media.

## Audiveris integration

Audiveris developers and contributors provide the OMR engine and editor used for score recognition. The application invokes the separately installed Java CLI with `-batch -export`, reads its MusicXML output, and adds this project's independent layout/navigation checks. It does not bundle or modify Audiveris source/binaries. MusicXML input can bypass Audiveris entirely.

License checked 2026-11-18: **AGPL-3.0-or-later**. The [upstream license](https://github.com/Audiveris/audiveris/blob/master/LICENSE) contains AGPL v3; the [Main.java header](https://github.com/Audiveris/audiveris/blob/master/app/src/main/java/org/audiveris/omr/Main.java) explicitly permits version 3 or a later version. Follow the upstream copyright/license notices, corresponding-source obligations and applicable modified-network-version provisions when distributing or modifying that software. Preserve its separate license; this repository's GPL-3.0-only notice does not relicense Audiveris.

Since v1.3.0, `installers/omr.json` pins official **Audiveris 5.11.0** macOS arm64 DMG / Windows x64 console MSI and SHA-256 values. [Corresponding upstream source](https://github.com/Audiveris/audiveris/tree/5.11.0) and [source archive](https://github.com/Audiveris/audiveris/archive/refs/tags/5.11.0.tar.gz). English `eng.traineddata` comes from [tessdata revision ced78752cc61322fb554c280d13360b35b8684e4](https://github.com/tesseract-ocr/tessdata/tree/ced78752cc61322fb554c280d13360b35b8684e4), under its [Apache-2.0 license](https://github.com/tesseract-ocr/tessdata/blob/ced78752cc61322fb554c280d13360b35b8684e4/LICENSE).

The installer downloads these distributions directly from upstream to the user's computer; it does not distribute them from this repository. It copies the unchanged app/runtime files, preserving upstream copyright notices, Java runtime `legal/` content and dependency licenses. It also downloads checksum-verified AGPL and Apache license texts to `licenses/` and records pinned download/source links in `sources.json` beside the managed installation. The official macOS disk image displays its AGPL notice; the automated installer continues past this prompt and preserves the full notice locally. This is not a waiver of upstream conditions.

Automated setup is not itself prohibited. If you change this distribution to ship native binaries, modified code, or a hosted service, review the applicable source, notice and network provisions for that arrangement. A source-only installer and separate CLI process do not provide a blanket exemption for other integrations or redistribution. Bundled Java/Tesseract/Leptonica and other Audiveris dependencies keep their own upstream terms.

## Guitar Pro input

[PyGuitarPro 0.11](https://pypi.org/project/PyGuitarPro/0.11/) by its upstream contributors reads GP3/4/5 files. Its source declares LGPL-3.0-only, retaining the upstream [license](https://github.com/Perlence/PyGuitarPro/blob/master/LICENSE). The installer retrieves the unmodified Python package and dependency metadata from PyPI into the external environment; this repository contains only its own adapter. Preserve the package notices and satisfy the applicable LGPL source and replacement/modification requirements if distributing the library with a binary application. This app's GPL-3.0-only license does not replace the library license or grant rights to any Guitar Pro score. No commercial scores or format samples are included.

## Media rights

Users must supply their own lawfully obtained recordings and sheet music, paying required charges or complying with an applicable free license. A purchase does not automatically grant rights to publish originals, scans, instrumental stems or other derived files. Project screenshots use synthetic demo audio and a project-authored demo score; no personal or commercial song is included.

## Verovio 6.4.1 — GP score engraving

[Verovio](https://github.com/rism-digital/verovio/tree/version-6.4.1) renders generated MusicXML to SVG. The separately installed Python wheel retains the upstream **LGPL-3.0-only** license and bundled font notices: [COPYING.LESSER](https://github.com/rism-digital/verovio/blob/version-6.4.1/COPYING.LESSER), [source and installation documentation](https://book.verovio.org/installing-or-building-from-sources/python.html). The one-click installer retrieves the pinned unmodified wheel into the external venv and verifies a synthetic engraving. This repository distributes its own MusicXML/SVG adapter, not a Verovio binary. If redistributing binaries, preserve all upstream notices and satisfy the LGPL's corresponding-source and user replacement/modification requirements. Engraving a score does not grant rights to distribute the source composition or score.

## Signalsmith Stretch Web 1.3.2 — tablet rate and pitch

[Signalsmith Stretch](https://github.com/Signalsmith-Audio/signalsmith-stretch) is **MIT licensed**, Copyright (c) 2022 Geraint Luff / Signalsmith Audio Ltd. The unmodified npm browser distribution (including embedded WebAssembly), exact package integrity and local SHA-256 inventory are in `web_player/vendor/`; the complete license is [Signalsmith-LICENSE.txt](web_player/vendor/Signalsmith-LICENSE.txt). Upstream package: [signalsmith-stretch 1.3.2](https://www.npmjs.com/package/signalsmith-stretch/v/1.3.2), [web API source](https://github.com/Signalsmith-Audio/signalsmith-stretch/tree/main/web).

The generated single-file player includes **two explicitly documented scheduling fixes** applied by `tools/build_web.py`: use an explicitly requested output timestamp when replacing future segments, and advance the current mapping only at actual current time. These changes let a scheduled count-in/start and exclusive loop-end stop coexist without discarding one another. The checked-in vendor source is unchanged; assertions and SHA-256 pins reject a changed upstream file before patching. The surrounding archive reader, UI and playback coordinator are original GPL-3.0-only project code. The MIT license remains applicable to the library, including these identified modified sections; no model weights are embedded in the web player.

The desktop key-change worker uses the already installed FFmpeg `asetrate`, `aresample`, `atempo`, padding and sample trimming filters. It never modifies the user's original recording. Qt's existing pitch-preserving speed playback remains independent of the key-change cache.


## Optional Session relay (v2.1)

- **aiohttp 3.14.5**: HTTP/WebSocket coordination and bounded file transfer. Package license expression **Apache-2.0 AND MIT**; its vendored llhttp carries MIT. [Upstream](https://github.com/aio-libs/aiohttp), [Apache license](https://github.com/aio-libs/aiohttp/blob/v3.14.5/LICENSE.txt). The official Python distribution retains the license files.
- **Segno 1.6.6**: invitation QR generation, **BSD-3-Clause**, copyright Lars Heuer. [Upstream and license](https://github.com/heuer/segno/blob/v1.6.6/LICENSE). No external QR image service receives invitation data.
- Relay transitive dependencies are pinned separately in `relay/requirements.txt` and retain their upstream license files. They are installed into the operator's external venv, not bundled into `.magic` or the browser HTML.
- The server deployment example uses **Caddy** as a separate HTTPS reverse proxy, **Apache-2.0**. [Source/license](https://github.com/caddyserver/caddy/blob/master/LICENSE). Caddy is not redistributed in this source ZIP. Operator credentials and TLS private keys are never included.

Session coordination code is original GPL-3.0-only code. These dependencies do not change the separate licenses of model weights, Audiveris or users' recordings/scores.
