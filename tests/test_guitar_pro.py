# SPDX-License-Identifier: GPL-3.0-only
"""Only project-authored notes are written; no third-party score fixtures."""

from pathlib import Path

import guitarpro
import pytest
from guitarpro import models as gp

from practice_player.cache import hash_file
from practice_player.config import Paths
from practice_player.guitar_pro import read_gp
from practice_player.score import ScoreTimeline, matching_musicxml
from practice_player.score_analysis import analyze_score, parse_notation
from practice_player.score_layout import analyze_scroll
from practice_player.score_view import view_profile
from tests.test_score_geometry import vector_pdf


def gp_song(path, count=4, version=(5, 1, 0), configure=lambda _: None):
    song = gp.Song(tempo=120)
    song.measureHeaders = []
    track = song.tracks[0]
    track.measures = []
    for index in range(count):
        header = gp.MeasureHeader(number=index + 1, start=960 + index * 3840)
        song.measureHeaders.append(header)
        measure = gp.Measure(track, header)
        track.measures.append(measure)
        beat = gp.Beat(measure.voices[0], start=header.start,
                       duration=gp.Duration(value=1), status=gp.BeatStatus.normal)
        beat.notes = [gp.Note(beat, value=index + 1, string=1, type=gp.NoteType.normal)]
        measure.voices[0].beats.append(beat)
    configure(song)
    guitarpro.write(song, path, version=version)
    return path


def converted(path):
    data, metadata = read_gp(path)
    xml = path.with_suffix(".musicxml")
    xml.write_bytes(data)
    return xml, metadata


@pytest.mark.parametrize("version", [(3, 0, 0), (4, 0, 6), (5, 1, 0)])
def test_native_formats_pitch_meter_and_repeats(tmp_path, version):
    def configure(song):
        song.tracks[0].offset = 2
        song.measureHeaders[0].isRepeatOpen = True
        song.measureHeaders[1].repeatClose = 1

    path = gp_song(tmp_path / f"study.gp{version[0]}", version=version, configure=configure)
    original = path.read_bytes()
    xml, meta = converted(path)
    result = parse_notation(xml, 1)
    assert meta["measures"] == 4
    assert [c["source_index"] for c in result["cues"]] == [0, 1, 0, 1, 2, 3]
    assert result["beats"] == 24
    assert result["notes"][0][2] == [7]  # E4 open + fret 1 + capo 2 = G4
    assert path.read_bytes() == original


def test_native_voltas_and_segno_coda(tmp_path):
    def endings(song):
        h = song.measureHeaders
        h[0].isRepeatOpen = True
        h[1].repeatAlternative, h[1].repeatClose = 1, 1
        h[2].repeatAlternative = 2

    xml, _ = converted(gp_song(tmp_path / "endings.gp5", configure=endings))
    assert [c["source_index"] for c in parse_notation(xml, 1)["cues"]] == [0, 1, 0, 2, 3]

    def directions(song):
        h = song.measureHeaders
        h[0].direction = gp.DirectionSign("Segno")
        h[1].fromDirection = gp.DirectionSign("Da Coda")
        h[2].fromDirection = gp.DirectionSign("Da Segno al Coda")
        h[3].direction = gp.DirectionSign("Coda")

    xml, _ = converted(gp_song(tmp_path / "coda.gp5", configure=directions))
    assert [c["source_index"] for c in parse_notation(xml, 1)["cues"]] == [0, 1, 2, 0, 1, 3]


def test_tuplets_and_ties_retain_duration(tmp_path):
    def configure(song):
        voice = song.tracks[0].measures[0].voices[0]
        voice.beats = []
        for i in range(3):
            beat = gp.Beat(voice, start=960 + i * 1280, status=gp.BeatStatus.normal,
                           duration=gp.Duration(value=2, tuplet=gp.Tuplet(3, 2)))
            beat.notes = [gp.Note(beat, value=1, string=1,
                                 type=gp.NoteType.tie if i else gp.NoteType.normal)]
            voice.beats.append(beat)

    xml, _ = converted(gp_song(tmp_path / "tuplets.gp5", count=1, configure=configure))
    result = parse_notation(xml, 1)
    assert result["beats"] == 4
    assert [n[0] for n in result["notes"]] == pytest.approx([0, 4 / 3, 8 / 3])
    assert [n[1] for n in result["notes"]] == pytest.approx([4 / 3] * 3)


def test_invalid_track_and_unsupported_direction_fail_visibly(tmp_path):
    path = gp_song(tmp_path / "study.gp5")
    with pytest.raises(ValueError, match="트랙 번호"):
        read_gp(path, 2)
    def configure(song):
        song.measureHeaders[0].direction = gp.DirectionSign("Double Coda")
    path = gp_song(path, configure=configure)
    with pytest.raises(ValueError, match="지원하지 않는"):
        read_gp(path)


def test_gp_pairs_with_mixed_pdf_and_reuses_reviewed_timeline(tmp_path, tone, monkeypatch):
    paths = Paths(tmp_path / "project")
    paths.create()
    pdf = vector_pdf(paths.score_targets / "study.pdf", [
        ((600, 800), [(600, [50, 180, 400, 550], [])], [5, 6]),
        ((600, 800), [(600, [50, 550], [])], [5, 6]),
    ])
    source = gp_song(pdf.with_suffix(".gp5"))
    before = hash_file(pdf), hash_file(source)
    assert matching_musicxml(pdf) == source
    monkeypatch.setattr("practice_player.score_analysis.run_omr",
                        lambda *_: pytest.fail("GP must bypass OMR"))
    path = analyze_score(tone, pdf, paths, mode="tempo")
    plan = ScoreTimeline.load(path)
    assert [c["page"] for c in plan.cues] == [0, 0, 0, 1]
    assert plan.data["notation_source"]["sha256"] == before[1]
    assert all(p["tab_companion"] for p in plan.data["pdf_layout"]["pages"])
    plan.data.update(reviewed=True, offset=0.125)
    plan.save(path)
    assert analyze_score(tone, pdf, paths, mode="tempo") == path
    view = analyze_scroll(tone, pdf, paths, path, view_profile(2, 600, 400))
    assert ScoreTimeline.load(view).data["offset"] == 0.125
    assert (hash_file(pdf), hash_file(source)) == before
    assert Path(plan.data["musicxml_path"]).is_file()
    gp_song(source, count=3)
    with pytest.raises(ValueError, match="4마디.*3마디"):
        analyze_score(tone, pdf, paths, mode="tempo")
    assert ScoreTimeline.load(path).data["reviewed"]
