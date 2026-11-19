# SPDX-License-Identifier: GPL-3.0-only
import json
import zipfile
from copy import deepcopy
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from guitarpro import models as gp

from practice_player.cache import hash_file
from practice_player.config import Paths
from practice_player.gp_render import native_layout, prepare_gp
from practice_player.guitar_pro import list_tracks
from practice_player.magic_bundle import export_magic
from practice_player.score import ScoreTimeline
from practice_player.score_analysis import analyze_score
from practice_player.score_layout import analyze_scroll
from practice_player.score_view import view_profile
from practice_player.transpose import prepare_pitch
from tests.test_guitar_pro import gp_song
from tests.test_score_geometry import vector_pdf


def portable_study(root, kind="gp", count=8):
    paths = Paths(root)
    paths.create()
    audio = paths.targets / "Practice Study.wav"
    rate = 44100
    times = np.arange(rate * count * 2) / rate
    tone = (0.16 * np.sin(2 * np.pi * 220 * times)).astype(np.float32)
    sf.write(audio, np.column_stack([tone, tone]), rate, subtype="PCM_16")

    def tracks(song):
        song.title = "Practice Study"
        song.tracks[0].name = "Rhythm Guitar"
        for i, measure in enumerate(song.tracks[0].measures):
            voice = measure.voices[0]
            voice.beats = []
            for beat_index in range(4):
                beat = gp.Beat(
                    voice,
                    start=measure.header.start + 960 * beat_index,
                    duration=gp.Duration(value=4),
                    status=gp.BeatStatus.normal,
                )
                beat.notes = [
                    gp.Note(beat, value=(i + beat_index) % 7, string=1, type=gp.NoteType.normal)
                ]
                voice.beats.append(beat)
        other = deepcopy(song.tracks[0])
        other.song, other.number, other.name = song, 2, "Lead Guitar"
        song.tracks.append(other)

    source = gp_song(paths.score_targets / "Practice Study.gp5", count=count, configure=tracks)
    prepared = prepare_gp(source, paths, 2)
    if kind == "gp":
        display = {k: prepared[k] for k in ("pdf", "layout", "source", "signature")}
        pdf = Path(prepared["pdf"])
    else:
        display = None
        pdf = paths.score_targets / "Practice Study.pdf"
        vector_pdf(
            pdf,
            [
                (
                    (595, 842),
                    [(700 - i * 150, [50, 170, 290, 410, 540], []) for i in range(count // 4)],
                    [5],
                )
            ],
        )
    timeline = analyze_score(
        audio, pdf, paths, musicxml=source, gp_track=2, gp_display=display, mode="tempo", bpm=120
    )
    timeline = analyze_scroll(audio, pdf, paths, timeline, view_profile("width", 900, 700))
    request = {
        "audio": str(audio),
        "instrumental": str(audio),
        "pdf": str(pdf),
        "timeline": str(timeline),
        "output": str(root / f"study-{kind}.magic"),
        "title": "Practice Study",
        "practice": {"bpm": 120},
        "pitch_semitones": 0,
    }
    return request, prepared, paths


@pytest.mark.parametrize("kind", ["gp", "pdf"])
def test_portable_score_keeps_originals_and_safe_metadata(tmp_path, kind):
    request, prepared, _ = portable_study(tmp_path, kind)
    original = hash_file(Path(request["audio"]))
    output = export_magic(request)
    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None
        assert all(info.compress_type == zipfile.ZIP_STORED for info in archive.infolist())
        manifest = json.loads(archive.read("manifest.json"))
        timing = json.loads(archive.read("timeline.json"))
        assert manifest["score"]["kind"] == kind
        assert len(timing["cues"]) == 8
        assert manifest["audio"]["duration"] == 16
        if kind == "gp":
            assert manifest["score"]["part"] == "Lead Guitar"
            assert archive.read("score/source.gp5") == Path(prepared["source"]).read_bytes()
        assert str(tmp_path) not in archive.read("manifest.json").decode()
        assert str(tmp_path) not in archive.read("timeline.json").decode()
        for cue in timing["cues"]:
            assert 0 <= cue["box"]["left"] < cue["box"]["right"] <= 1
    assert hash_file(Path(request["audio"])) == original
    with pytest.raises(FileExistsError):
        export_magic(request)


def test_gp_native_parts_and_geometry_without_omr(tmp_path, monkeypatch):
    def forbidden(*_, **__):
        raise AssertionError("A native GP score must not use OMR or rediscover PDF barlines")

    monkeypatch.setattr("practice_player.score_analysis.run_omr", forbidden)
    monkeypatch.setattr("practice_player.score_layout.analyze_layout", forbidden)
    request, prepared, paths = portable_study(tmp_path, count=32)
    tracks = list_tracks(Path(prepared["source"]))
    assert [t["name"] for t in tracks] == ["Rhythm Guitar", "Lead Guitar"]
    sizes, bars, layout = native_layout(prepared)
    assert len(sizes) >= 2 and len(bars) == 32 and layout["method"] == "guitar-pro-native"
    timeline = ScoreTimeline.load(Path(request["timeline"]))
    assert timeline.data["scroll"]["layout_method"] == "guitar-pro-native"
    assert prepare_gp(Path(prepared["source"]), paths, 2)["cached"]
    first = prepare_gp(Path(prepared["source"]), paths, 1)
    assert first["pdf"] != prepared["pdf"]
    Path(prepared["source"]).write_bytes(b"changed")
    with pytest.raises(ValueError, match="GP 원본"):
        export_magic(request)


@pytest.mark.parametrize("semitones", [-12, 5, 12])
def test_pitch_changes_frequency_without_moving_time(tone, tmp_path, semitones):
    result = prepare_pitch(tone, Paths(tmp_path), semitones)
    samples, rate = sf.read(result)
    original, _ = sf.read(tone)
    assert len(samples) == len(original)
    segment = samples[rate // 2 : rate * 2, 0]
    frequency = np.fft.rfftfreq(len(segment), 1 / rate)[np.abs(np.fft.rfft(segment)).argmax()]
    reference = original[rate // 2 : rate * 2, 0]
    base = np.fft.rfftfreq(len(reference), 1 / rate)[np.abs(np.fft.rfft(reference)).argmax()]
    assert frequency == pytest.approx(base * 2 ** (semitones / 12), rel=0.015)
    assert prepare_pitch(tone, Paths(tmp_path), semitones) == result
    result.write_bytes(b"broken")
    assert sf.info(prepare_pitch(tone, Paths(tmp_path), semitones)).frames == len(original)


def test_gp_drum_part_renders_and_uses_tempo(tmp_path):
    def drums(song):
        song.tracks[0].name = "Drums"
        song.tracks[0].isPercussionTrack = True
        song.tracks[0].channel.channel = 9
        for m in song.tracks[0].measures:
            m.voices[0].beats[0].notes[0].value = 36

    source = gp_song(tmp_path / "drums.gp5", configure=drums)
    prepared = prepare_gp(source, Paths(tmp_path))
    assert prepared["metadata"]["percussion"]
    assert len(native_layout(prepared)[1]) == 4


def test_gui_native_part_selection_and_key_preserve_media_position(tmp_path):
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication

    from practice_player.gui import Window
    from tests.test_gui import wait_until

    request, prepared, paths = portable_study(tmp_path)
    app = QApplication.instance() or QApplication([])
    window = Window(paths)
    window.source_path = Path(request["audio"])
    window.pending_source = window.source_path
    window.source_name = "Practice Study"
    try:
        window.load_result(
            {
                "instrumental": request["instrumental"],
                "cached": True,
                "source_hash": hash_file(window.source_path),
            }
        )
        wait_until(app, lambda: window.worker is None)
        assert window.score_panel.gp_source == Path(prepared["source"])
        assert window.score_panel.parts.count() == 2
        window.score_panel.parts.setCurrentIndex(1)
        window.score_panel.parts.activated.emit(1)
        wait_until(app, lambda: window.worker is None)
        assert window.score_panel.parts.currentText() == "Lead Guitar"
        assert window.score_panel.timeline is not None
        window.audio_output.setVolume(0)
        wait_until(app, lambda: window.player.duration() > 0)
        window.seek_to(3000)
        window.key.setValue(5)
        window.change_key()
        wait_until(
            app,
            lambda: (
                window.worker is None
                and window._pitch_pending is None
                and window.player.duration() > 0
            ),
        )
        assert window.pitch_semitones == 5
        assert window.player.position() == pytest.approx(3000, abs=5)
        assert window.score_panel.timeline is not None
        assert window.player.duration() == pytest.approx(16000, abs=5)
        window.reset_key()
        wait_until(
            app,
            lambda: (
                window.worker is None
                and window._pitch_pending is None
                and window.player.duration() > 0
            ),
        )
        assert window.output_path == Path(request["instrumental"])
        assert window.player.position() == pytest.approx(3000, abs=5)
    finally:
        window.close()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
