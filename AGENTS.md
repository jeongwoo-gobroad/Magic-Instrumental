# Project instructions

Read `AI_CODER_README.md` before making changes. It contains user requirements,
architecture, external venv locations, validation commands, model provenance,
and remaining platform verification limits.

- Keep the application Python-based, with a usable desktop GUI on macOS and Windows.
- Never create a venv inside this project or install project packages globally.
- macOS venv: `~/.vocal-separator-and-speed-adj/.venv`.
- Windows venv: the single path in `venv-location.windows.txt`.
- Public Windows template: `venv-location.windows.example.txt`. Never publish the actual config.
- Keep one-click Python/dependency installers functional without global Python or admin access.
- Keep original audio under `targets/` untouched. Do not overwrite user exports silently.
- Cache by full file SHA-256 and processing signature, never title or filename.
- Include CUDA and Apple MPS support. Explicit GPU failures must be visible.
- Preserve pinned model revisions/checksums; bump pipeline version for output changes.
- Do not claim universal model superiority or Windows validation without evidence.
- Update both the human README and AI handoff documentation for behavior changes.
- Keep OMR/navigation/audio alignment in the pre-analysis worker, never playback callbacks.
- Score times and metronome origins are original media seconds; never apply playback rate twice.
- Preserve user PDF/audio files and reviewed score timelines; expose recognition uncertainty.
- Publish only reviewed files from `tools/build_release.py`; never media, caches, weights or logs.
- Own code/docs are GPL-3.0-only; model weights and dependencies retain upstream licenses.
- Maintain `CHANGELOG.md` and `practice_player.__version__` for user-visible releases.
