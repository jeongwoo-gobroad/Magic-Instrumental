# SPDX-License-Identifier: GPL-3.0-only
import json
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from practice_player.cache import hash_file
from practice_player.config import Paths, Settings
from practice_player.pipeline import process


def test_renamed_identical_bytes_reuse_cache_without_model(tone, tmp_path, fake_engine):
    paths = Paths(tmp_path / "project")
    first = process(tone, Settings(), paths, runner=fake_engine)
    renamed = tmp_path / "다른 제목.WAV"
    shutil.copyfile(tone, renamed)
    second = process(renamed, Settings(device="cuda"), paths, offline=True)
    assert second.cached and not first.cached
    assert second.instrumental == first.instrumental
    assert fake_engine.calls == 1
    assert first.instrumental.parent.parent.name == hash_file(tone)


def test_same_name_different_bytes_and_settings_are_separate(tone, tmp_path, fake_engine):
    paths = Paths(tmp_path / "project")
    original = process(tone, Settings(), paths, runner=fake_engine)
    # WAV accepts a trailing metadata-like change; content identity must still differ.
    with tone.open("ab") as output:
        output.write(b"different-content")
    changed = process(tone, Settings(), paths, runner=fake_engine)
    low_memory = process(tone, Settings(low_memory=True), paths, runner=fake_engine)
    other_model = process(tone, Settings(model="deux"), paths, runner=fake_engine)
    assert len({r.instrumental for r in [original, changed, low_memory, other_model]}) == 4
    assert fake_engine.calls == 4


@pytest.mark.parametrize("damage", ["truncated", "same_size", "manifest"])
def test_corrupt_cache_is_rebuilt(tone, tmp_path, fake_engine, damage):
    paths = Paths(tmp_path / "project")
    first = process(tone, Settings(), paths, runner=fake_engine)
    user_export = first.instrumental.parent / "practice_0.75x.wav"
    user_export.write_bytes(b"user-export-must-survive")
    if damage == "truncated":
        first.instrumental.write_bytes(b"broken")
    elif damage == "same_size":
        data = bytearray(first.instrumental.read_bytes())
        data[-1] ^= 1
        first.instrumental.write_bytes(data)
    else:
        (first.instrumental.parent / "manifest.json").write_text("{}")
    repaired = process(tone, Settings(), paths, runner=fake_engine)
    assert not repaired.cached and fake_engine.calls == 2
    assert user_export.read_bytes() == b"user-export-must-survive"
    assert process(tone, Settings(), paths, runner=fake_engine).cached


def test_failure_never_publishes_cache(tone, tmp_path):
    def failing(source, output, *_args, **_kwargs):
        output.write_bytes(b"partial")
        raise RuntimeError("simulated failure")

    paths = Paths(tmp_path / "project")
    with pytest.raises(RuntimeError, match="simulated"):
        process(tone, Settings(), paths, runner=failing)
    assert not list(paths.processed.rglob("manifest.json"))
    assert not list(paths.processed.rglob("instrumental.wav"))


def test_same_file_concurrent_jobs_only_separate_once(tone, tmp_path, fake_engine):
    paths = Paths(tmp_path / "project")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda _: process(tone, Settings(), paths, runner=fake_engine), range(2))
        )
    assert fake_engine.calls == 1
    assert sorted(result.cached for result in results) == [False, True]


def test_source_changed_during_snapshot_does_not_publish(tone, tmp_path, monkeypatch):
    copy = shutil.copyfile

    def mutated(source, dest):
        copy(source, dest)
        with dest.open("ab") as output:
            output.write(b"mutation")

    monkeypatch.setattr("practice_player.pipeline.shutil.copyfile", mutated)
    paths = Paths(tmp_path / "project")
    with pytest.raises(RuntimeError, match="변경"):
        process(tone, Settings(), paths)
    assert not list(paths.processed.rglob("manifest.json"))


def test_cli_cache_hit_runs_offline_without_importing_engine(tone, tmp_path, fake_engine):
    import subprocess

    from practice_player.config import ROOT

    paths = Paths(tmp_path / "project")
    process(tone, Settings(), paths, runner=fake_engine)
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "main.py"),
            "separate",
            "--input",
            str(tone),
            "--root",
            str(paths.root),
            "--model",
            "hyperace",
            "--device",
            "cuda",
            "--offline",
            "--events",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    event = json.loads(result.stdout.split("@@PRACTICE@@")[-1])
    assert event["type"] == "result" and event["cached"]
