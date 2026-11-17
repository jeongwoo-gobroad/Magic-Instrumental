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
| [pypdfium2](https://github.com/pypdfium2-team/pypdfium2) | PDF rendering | BSD-3-Clause / Apache-2.0 plus PDFium and bundled third-party licenses |
| [pypdf](https://github.com/py-pdf/pypdf) | PDF structure | BSD-3-Clause |
| [music21](https://github.com/cuthbertLab/music21) | MusicXML and notation | BSD-3-Clause; this app does not distribute its example music corpus |
| [librosa](https://github.com/librosa/librosa) | Offline audio alignment | ISC |
| [NumPy](https://github.com/numpy/numpy) | Numeric processing | BSD-3-Clause and bundled notices |
| [SoundFile](https://github.com/bastibe/python-soundfile) | Audio files | BSD-3-Clause; bundled libsndfile has its own LGPL terms |
| [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg) | FFmpeg executable distribution | BSD-2-Clause wrapper; **FFmpeg binary licensing is separate** |
| [FFmpeg](https://ffmpeg.org/legal.html) | Decode and tempo export | LGPL/GPL and optional codec terms depend on the actual build. Inspect `ffmpeg -L` and its distributor's source information before redistributing binaries. |
| [filelock](https://github.com/tox-dev/filelock) | Cache locks | Unlicense |
| [defusedxml](https://github.com/tiran/defusedxml) | XML safety | PSF-2.0 |
| [uv](https://github.com/astral-sh/uv) | Bootstrap only | MIT OR Apache-2.0; downloaded as a checksum-pinned executable |
| [CPython](https://docs.python.org/3/license.html) / [python-build-standalone](https://github.com/astral-sh/python-build-standalone) | Private Python runtime | Python license and distribution dependency notices; uv downloads this runtime separately |
| [Audiveris](https://github.com/Audiveris/audiveris) | Optional PDF music recognition | AGPL-3.0; separate native application, installed by the user |
| [Tesseract OCR data](https://github.com/tesseract-ocr/tessdata) | Optional English OCR | Apache-2.0; installed separately for Audiveris |

A binary repackager must review and satisfy the licenses of the actual dependencies shipped, including source/notice obligations. This source-only release does not grant rights to redistribute third-party model weights or user media.

## Media rights

Users must supply their own lawfully obtained recordings and sheet music, paying required charges or complying with an applicable free license. A purchase does not automatically grant rights to publish originals, scans, instrumental stems or other derived files. Project screenshots use synthetic demo audio and a project-authored demo score; no personal or commercial song is included.
