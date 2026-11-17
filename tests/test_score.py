# SPDX-License-Identifier: GPL-3.0-only
import json
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from pypdf import PdfWriter

from practice_player.cache import hash_file
from practice_player.config import Paths
from practice_player.score import ScoreTimeline, load_last_timeline, matching_score
from practice_player.score_analysis import analyze_score, parse_notation, parse_route_override


def xml_file(tmp_path, bodies):
    header = (
        "<attributes><divisions>1</divisions><time><beats>4</beats>"
        "<beat-type>4</beat-type></time></attributes>"
    )
    note = (
        "<note><pitch><step>C</step><octave>4</octave></pitch>"
        "<duration>4</duration><type>whole</type></note>"
    )
    measures = "".join(
        f'<measure number="{i + 1}">{header if i == 0 else ""}{body}{note}</measure>'
        for i, body in enumerate(bodies)
    )
    path = tmp_path / "song.musicxml"
    path.write_text(
        '<score-partwise version="4.0"><part-list><score-part id="P1">'
        '<part-name>Piano</part-name></score-part></part-list><part id="P1">'
        + measures
        + "</part></score-partwise>"
    )
    return path


def test_open_volta_with_ds_coda_keeps_second_ending_and_page_returns(tmp_path):
    start = '<barline location="left"><repeat direction="forward"/></barline>'
    first = '<barline location="left"><ending number="1" type="start"/></barline>'
    first += '<barline location="right"><ending number="1" type="discontinue"/></barline>'
    end = '<barline location="right"><repeat direction="backward"/></barline>'
    second = '<barline location="left"><ending number="2" type="start"/></barline>'
    second += '<barline location="right"><ending number="2" type="stop"/></barline>'

    def direction(attrs):
        return f"<direction><direction-type><words/></direction-type><sound {attrs}/></direction>"

    path = xml_file(
        tmp_path,
        [
            start + direction('segno="s"'),
            direction('tocoda="c"'),
            first + '<print new-page="yes"/>',
            end,
            second + direction('dalsegno="s"'),
            direction('coda="c"'),
        ],
    )
    notation = parse_notation(path, 2)
    assert [x["source_index"] for x in notation["cues"]] == [0, 1, 2, 3, 0, 1, 4, 0, 1, 5]
    assert [x["page"] for x in notation["cues"]] == [0, 0, 1, 1, 0, 0, 1, 0, 0, 1]
    assert notation["beats"] == 40


def test_dacapo_fine_and_bad_segno(tmp_path):
    path = xml_file(
        tmp_path,
        [
            "",
            "<direction><direction-type><words>Fine</words></direction-type></direction>",
            "<direction><direction-type><words>D.C. al Fine</words></direction-type></direction>",
        ],
    )
    assert [x["measure"] for x in parse_notation(path, 1)["cues"]] == ["1", "2", "3", "1", "2"]
    path = xml_file(
        tmp_path,
        ["", "<direction><direction-type><words>D.S.</words></direction-type></direction>"],
    )
    with pytest.raises(ValueError, match="세뇨"):
        parse_notation(path, 1)
    assert len(parse_notation(path, 1, "1-2,1")["cues"]) == 3


@pytest.mark.parametrize("text", ["0-3", "4-2", "1-99", "1;2", ""])
def test_invalid_route_rejected(text):
    with pytest.raises(ValueError):
        parse_route_override(text, 8)


def plan_data():
    return {
        "version": 1,
        "page_count": 3,
        "cues": [{"start": 0, "page": 0}, {"start": 10, "page": 2}, {"start": 20, "page": 0}],
        "end": 30,
    }


def test_timeline_media_clock_seek_and_offset():
    plan = ScoreTimeline(plan_data())
    assert [plan.index_at(t) for t in [-1, 0, 9.9, 10, 20, 30, 2]] == [None, 0, 0, 1, 2, None, 0]
    assert plan.index_at(11, offset=2) == 0
    assert plan.index_at(12, offset=2) == 1


@pytest.mark.parametrize(
    "field,value", [("start", float("nan")), ("start", -1), ("start", 10), ("page", 3)]
)
def test_invalid_timeline_is_not_cached(field, value):
    data = plan_data()
    data["cues"][2][field] = value
    with pytest.raises(ValueError):
        ScoreTimeline(data)


def test_analysis_cache_is_by_content_and_preserves_edits(tmp_path, tone, monkeypatch):
    paths = Paths(tmp_path / "project")
    paths.create()
    pdf = paths.score_targets / "song.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    with pdf.open("wb") as file:
        writer.write(file)
    xml = xml_file(paths.score_targets, ["", ""])

    def no_omr(*_):
        raise AssertionError("MusicXML must bypass OMR")

    monkeypatch.setattr("practice_player.score_analysis.run_omr", no_omr)
    path = analyze_score(tone, pdf, paths, mode="tempo", bpm=120)
    plan = ScoreTimeline.load(path)
    plan.data["offset"] = 0.123
    plan.save(path)
    assert analyze_score(tone, pdf, paths, mode="tempo", bpm=120) == path
    assert ScoreTimeline.load(path).data["offset"] == 0.123
    renamed = tone.with_name("renamed.wav")
    renamed.write_bytes(tone.read_bytes())
    assert analyze_score(renamed, pdf, paths, mode="tempo", bpm=120) == path
    assert load_last_timeline(pdf, paths, hash_file(tone))[0] == path
    xml.write_text(xml.read_text().replace("<step>C</step>", "<step>D</step>"))
    assert analyze_score(tone, pdf, paths, mode="tempo", bpm=120) != path
    assert path.exists()
    with pdf.open("ab") as file:
        file.write(b"\n% changed PDF bytes\n")
    assert load_last_timeline(pdf, paths, hash_file(tone)) is None
    assert matching_score(Path("SONG.MP3"), paths) == pdf.resolve()


def test_real_chroma_alignment_tracks_known_notes(tmp_path):
    from practice_player.score_analysis import align_audio

    rate = 11025
    pitches = [60, 64, 67, 62, 69, 65, 71, 60]
    lead = 0.8
    parts = [np.zeros(round(rate * lead))]
    notes = []
    for i, pitch in enumerate(pitches):
        t = np.arange(round(rate * 0.6)) / rate
        frequency = 440 * 2 ** ((pitch - 69) / 12)
        parts.append(0.2 * np.sin(2 * np.pi * frequency * t))
        notes.append((i, 1, [pitch % 12]))
    audio = tmp_path / "alignment.wav"
    sf.write(audio, np.concatenate(parts), rate)
    starts, end, metrics = align_audio(
        audio,
        {"beats": 8, "notes": notes, "cues": [{"beat": i} for i in range(8)]},
        lambda *_: None,
    )
    assert np.max(np.abs(np.array(starts[1:]) - (lead + np.arange(1, 8) * 0.6))) < 0.35
    assert 5 < end < 5.7
    assert metrics["similarity"] > 0.7


def test_pdf_gui_follows_cached_pages_and_splitter_restores(tmp_path, tone):
    from PySide6.QtWidgets import QApplication

    from practice_player.gui import Window

    app = QApplication.instance() or QApplication([])
    paths = Paths(tmp_path / "project")
    paths.create()
    pdf = paths.score_targets / (tone.stem + ".pdf")
    writer = PdfWriter()
    for _ in range(3):
        writer.add_blank_page(width=595, height=842)
    with pdf.open("wb") as file:
        writer.write(file)
    window = Window(paths)
    window.show()
    app.processEvents()
    try:
        window.score_panel.bind_track(tone, hash_file(tone))
        assert window.score_panel.pdf == pdf.resolve()
        data = plan_data()
        for i, cue in enumerate(data["cues"]):
            cue.update(source_index=i, measure=str(i + 1), visit=1, marks=[])
        data.update(audio_sha256=hash_file(tone), pdf_sha256=hash_file(pdf))
        path = tmp_path / "timeline.json"
        path.write_text(json.dumps(data))
        window.score_panel.load_timeline(path)
        window.position_changed(11000)
        assert window.score_panel.view.page == 2
        window.speed.setValue(50)
        window.position_changed(11000)
        assert window.score_panel.view.page == 2
        window.position_changed(21000)
        assert window.score_panel.view.page == 0
        window.main_splitter.setSizes([550, 900])
        app.processEvents()
        sizes = window.main_splitter.sizes()
    finally:
        window.close()
        app.processEvents()
    other = Window(paths)
    other.show()
    app.processEvents()
    try:
        assert abs(other.main_splitter.sizes()[0] - sizes[0]) <= 2
    finally:
        other.close()
        app.processEvents()
