# SPDX-License-Identifier: GPL-3.0-only
import numpy as np
import pytest
import soundfile as sf

from practice_player.audio import decode, discover, export_tempo, run_ffmpeg, tempo_filter


@pytest.mark.parametrize("speed", [0.25, 0.5, 0.75, 1.0, 1.5, 2.0])
def test_export_duration_and_pitch(tone, tmp_path, speed):
    destination = export_tempo(tone, tmp_path / "느린 연습.flac", speed)
    samples, rate = sf.read(destination)
    assert abs(len(samples) / rate - 3 / speed) < 0.15
    spectrum = np.abs(np.fft.rfft(samples[:, 0]))
    peak = np.fft.rfftfreq(len(samples), 1 / rate)[np.argmax(spectrum)]
    assert abs(peak - 440) < 2


@pytest.mark.parametrize("value", [0, -1, 2.1, float("nan"), float("inf")])
def test_invalid_speed(value):
    with pytest.raises(ValueError):
        tempo_filter(value)


@pytest.mark.parametrize("extension", ["mp3", "m4a", "flac", "ogg", "aiff", "wav"])
def test_real_ffmpeg_formats(tone, tmp_path, extension):
    encoded = tmp_path / f"변환 음원.{extension}"
    run_ffmpeg(["-i", str(tone), str(encoded)])
    output = tmp_path / "decoded.wav"
    decode(encoded, output)
    info = sf.info(output)
    assert info.channels == 2 and info.samplerate == 44100
    assert abs(info.duration - 3) < 0.15


def test_discover_nested_case_insensitive_and_bad_file(tmp_path):
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "Song.MP3").touch()
    (tmp_path / "readme.txt").touch()
    assert discover(tmp_path) == [nested / "Song.MP3"]
    with pytest.raises(RuntimeError, match="변환 실패"):
        decode(nested / "Song.MP3", tmp_path / "bad.wav")


def test_export_cannot_overwrite_cache(tone):
    with pytest.raises(ValueError, match="덮어쓸"):
        export_tempo(tone, tone, 0.5)
    existing = tone.with_name("existing.wav")
    existing.write_bytes(b"preserve")
    with pytest.raises(FileExistsError):
        export_tempo(tone, existing, 0.5)
    assert existing.read_bytes() == b"preserve"
    export_tempo(tone, existing, 0.5, overwrite=True)
    assert sf.info(existing).duration > 5.8
