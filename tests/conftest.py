# SPDX-License-Identifier: GPL-3.0-only
import os
import shutil
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_MEDIA_BACKEND", "ffmpeg")

import numpy as np
import pytest
import soundfile as sf


@pytest.fixture
def tone(tmp_path):
    rate = 44100
    time = np.arange(rate * 3) / rate
    audio = np.column_stack([np.sin(2 * np.pi * 440 * time) * 0.1] * 2)
    path = tmp_path / "연습 음원.wav"
    sf.write(path, audio, rate, subtype="FLOAT")
    return path


@pytest.fixture
def fake_engine():
    class Engine:
        calls = 0

        def __call__(self, source: Path, output: Path, *_args, **_kwargs):
            self.calls += 1
            shutil.copyfile(source, output)
            return "test"

    return Engine()
