# SPDX-License-Identifier: GPL-3.0-only
import json
import os
import shutil
import subprocess
import sys

import numpy as np
import pytest
import soundfile as sf

from practice_player.config import ROOT


@pytest.mark.integration
@pytest.mark.skipif(os.environ.get("RUN_MODEL_TESTS") != "1", reason="Real weights opt-in")
@pytest.mark.parametrize("model", ["hyperace", "deux", "leap", "sw6", "mega"])
@pytest.mark.parametrize("low_memory", [False, True])
def test_real_model(tone, tmp_path, model, low_memory):
    # Cross the actual chunk boundary, including the final partial chunk.
    duration = 9 if low_memory else 23
    timeline = np.arange(44100 * duration) / 44100
    sf.write(
        tone,
        np.column_stack([0.1 * np.sin(2 * np.pi * 440 * timeline)] * 2),
        44100,
        subtype="FLOAT",
    )

    # Same worker process boundary as the GUI. Reuse only the verified model assets.
    def link_or_copy(source, destination):
        try:
            return os.link(source, destination)
        except OSError:
            return shutil.copy2(source, destination)

    shutil.copytree(ROOT / "models", tmp_path / "models", copy_function=link_or_copy)
    args = [
        sys.executable,
        str(ROOT / "main.py"),
        "separate",
        "--input",
        str(tone),
        "--root",
        str(tmp_path),
        "--model",
        model,
        "--all-variants",
        "--events",
    ]
    if low_memory:
        args.append("--low-memory")
    if model == "mega":
        args.extend(["--variant", "keys"])
    result = subprocess.run(args, capture_output=True, text=True, timeout=600, encoding="utf-8")
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-6000:]
    event = json.loads(result.stdout.split("@@PRACTICE@@")[-1])
    assert event["device"] in {"mps", "cuda", "cpu"}
    assert not event["cached"]
    assert event["source_frames"] == 44100 * duration
    expected = {"no_vocals", "vocals", "original"}
    if model == "sw6":
        expected.update({"guitar", "no_guitar", "drums", "no_drums", "bass", "no_bass",
                         "piano", "no_piano"})
    elif model == "mega":
        expected = {"keys", "no_keys", "synth", "no_synth", "original"}
    assert set(event["variants"]) == expected
    for option in event["variants"].values():
        assert option["frames"] == 44100 * duration
    hit = subprocess.run(
        args + ["--offline"], capture_output=True, text=True, timeout=20, encoding="utf-8"
    )
    assert hit.returncode == 0, hit.stderr
    assert json.loads(hit.stdout.split("@@PRACTICE@@")[-1])["cached"]
