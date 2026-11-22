# SPDX-License-Identifier: GPL-3.0-only
import json

import numpy as np
import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject

from practice_player.cache import hash_file
from practice_player.config import Paths
from practice_player.score import ScoreTimeline
from practice_player.score_analysis import analyze_score
from practice_player.score_layout import analyze_scroll, detect_layout, staff_boxes
from practice_player.score_view import rendered_size, view_profile


@pytest.fixture
def score_fixture(tmp_path, tone):
    paths = Paths(tmp_path / "project")
    paths.create()
    pdf = paths.score_targets / "fixture.pdf"
    writer = PdfWriter()
    page = writer.add_blank_page(width=595, height=842)
    commands = ["0 G 0.6 w"]
    for top in (650, 450, 250):
        for line in range(5):
            y = top - line * 6
            commands.append(f"40 {y} m 555 {y} l S")
    stream = DecodedStreamObject()
    stream.set_data("\n".join(commands).encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    with pdf.open("wb") as output:
        writer.write(output)
    xml = pdf.with_suffix(".musicxml")
    measures = []
    for index in range(6):
        body = ""
        if index == 0:
            body += (
                "<attributes><divisions>1</divisions><time><beats>4</beats>"
                "<beat-type>4</beat-type></time></attributes>"
                '<barline location="left"><repeat direction="forward"/></barline>'
            )
        if index in (2, 4):
            body += '<print new-system="yes"/>'
        if index == 5:
            body += '<barline location="right"><repeat direction="backward"/></barline>'
        body += (
            "<note><pitch><step>C</step><octave>4</octave></pitch>"
            "<duration>4</duration><type>whole</type></note>"
        )
        measures.append(f'<measure number="{index + 1}" width="100">{body}</measure>')
    xml.write_text(
        '<score-partwise version="4.0"><part-list><score-part id="P1">'
        '<part-name>Piano</part-name></score-part></part-list><part id="P1">'
        + "".join(measures)
        + "</part></score-partwise>"
    )
    base = analyze_score(tone, pdf, paths, mode="tempo", bpm=120)
    plan = ScoreTimeline.load(base)
    plan.data.update(offset=0.125, reviewed=True)
    plan.save(base)
    return paths, tone, pdf, xml, base


def test_pdf_geometry_and_scroll_follow_repeat_without_realigning(score_fixture, monkeypatch):
    paths, audio, pdf, xml, base = score_fixture
    sizes, bars = detect_layout(pdf, xml)
    assert sizes == [[595, 842]]
    assert len(bars) == 6
    assert bars[0]["top"] < bars[2]["top"] < bars[4]["top"]
    assert bars[0]["right"] == pytest.approx(bars[1]["left"])
    monkeypatch.setattr(
        "practice_player.score_analysis.align_audio",
        lambda *_: pytest.fail("Zoom-only analysis must reuse timing"),
    )
    profile = view_profile(2, 420, 360)
    path = analyze_scroll(audio, pdf, paths, base, profile)
    plan = ScoreTimeline.load(path)
    assert plan.data["reviewed"] and plan.data["offset"] == 0.125
    assert plan.cues == ScoreTimeline.load(base).cues
    assert plan.scroll_at(0.1) is None
    early, middle, bottom, back = [plan.scroll_at(t) for t in (1, 5, 11, 13)]
    assert early[2] < middle[2] < bottom[2]
    assert back[2] == early[2]  # first measure, second visit
    # Forward pan inside a bar and no extra speed multiplier in media-time lookup.
    assert plan.scroll_at(1.8)[1] > plan.scroll_at(0.5)[1]
    assert analyze_scroll(audio, pdf, paths, base, profile) == path
    assert analyze_scroll(audio, pdf, paths, path, profile) == path
    enlarged = analyze_scroll(audio, pdf, paths, path, view_profile(3, 420, 360))
    resized = analyze_scroll(audio, pdf, paths, path, view_profile(2, 620, 360))
    assert len({path, enlarged, resized}) == 3
    assert ScoreTimeline.load(enlarged).cues == plan.cues
    assert base.exists()


def test_staff_detection_rejects_missing_or_wrong_lines():
    with pytest.raises(ValueError, match="확정"):
        staff_boxes(np.full((500, 400), 255, dtype=np.uint8), [5])
    image = np.full((500, 400), 255, dtype=np.uint8)
    for y in (50, 60, 70, 80, 100):
        image[y, 20:380] = 0
    with pytest.raises(ValueError, match="불규칙"):
        staff_boxes(image, [5])


@pytest.mark.parametrize(
    "zoom,width,height",
    [(0, 400, 500), (7, 400, 500), ("nan", 400, 500), (2, 0, 500), (2, 400, 90)],
)
def test_invalid_view_settings(zoom, width, height):
    with pytest.raises(ValueError):
        view_profile(zoom, width, height)


def test_scroll_plan_validation_and_changed_source(score_fixture):
    paths, audio, pdf, _, base = score_fixture
    path = analyze_scroll(audio, pdf, paths, base, view_profile(2, 420, 360))
    data = json.loads(path.read_text())
    data["scroll"]["frames"][0]["y0"] = float("nan")
    with pytest.raises(ValueError, match="범위"):
        ScoreTimeline(data)
    changed = audio.with_name("changed.wav")
    changed.write_bytes(audio.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="다릅니다"):
        analyze_scroll(changed, pdf, paths, base, view_profile(2, 420, 360))


def test_scroll_uses_hash_verified_relocated_xml_without_rewriting_reviewed_base(score_fixture):
    paths, audio, pdf, xml, base = score_fixture
    original = base.read_bytes()
    moved = xml.rename(xml.with_name("reviewed-notation-relocated.musicxml"))
    different = xml.with_name("different.musicxml")
    different.write_text("<score-partwise/>")
    profile = view_profile(2, 420, 360)
    with pytest.raises(ValueError, match="같은 MusicXML"):
        analyze_scroll(audio, pdf, paths, base, profile, musicxml=different)
    path = analyze_scroll(audio, pdf, paths, base, profile, musicxml=moved)
    result = ScoreTimeline.load(path)
    assert result.data["musicxml_path"] == str(moved.resolve())
    assert result.data["xml_sha256"] == hash_file(moved)
    assert result.data["reviewed"] and result.data["offset"] == 0.125
    assert result.cues == ScoreTimeline.load(base).cues
    assert base.read_bytes() == original


def test_gui_saved_scroll_resize_zoom_pause_and_seek(score_fixture, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from practice_player.gui import STYLE, Window
    from tests.test_gui import wait_until

    paths, audio, pdf, _, base = score_fixture
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(STYLE)
    window = Window(paths)
    # Keep status text on one line so zoom restoration uses the same viewport.
    window.resize(1500, 930)
    window.show()
    panel = window.score_panel
    try:
        panel.bind_track(audio, hash_file(audio))
        panel.open_pdf(pdf)
        panel.zoom.setCurrentIndex(panel.zoom.findData(2.0))
        app.processEvents()
        profile = panel.view.profile()
        path = analyze_scroll(audio, pdf, paths, base, profile)
        panel.load_timeline(path)
        app.processEvents()
        assert panel.view_matches(), (profile, panel.view.profile())
        panel.on_position(11000)
        x, y = panel.timeline.scroll_at(11)[1:]
        assert panel.view.verticalScrollBar().value() == y
        assert panel.view.horizontalScrollBar().value() == x
        window.speed.setValue(50)
        panel.on_position(11000)
        assert panel.view.verticalScrollBar().value() == y
        panel.on_position(13000)
        assert panel.view.verticalScrollBar().value() < y
        # At 16ms playback ticks only saved coordinates are used, no layout detection.
        monkeypatch.setattr(
            "practice_player.score_layout.detect_layout",
            lambda *_: pytest.fail("Playback must not analyze PDF"),
        )
        from PySide6.QtCore import QUrl

        window.player.setSource(QUrl.fromLocalFile(str(audio)))
        window.audio_output.setVolume(0)
        window.player.play()
        wait_until(app, lambda: panel.camera_timer.isActive())
        window.player.pause()
        wait_until(app, lambda: not panel.camera_timer.isActive())
        panel.zoom.setCurrentIndex(panel.zoom.findData(3.0))
        assert not panel.view_matches()
        before = panel.view.verticalScrollBar().value()
        panel.on_position(11000)
        assert panel.view.verticalScrollBar().value() == before
        assert "재분석" in panel.view_status.text()
        panel.zoom.setCurrentIndex(panel.zoom.findData(2.0))
        assert panel.view_matches(), (
            profile, panel.view.profile(), panel.status.height(), panel.status.text(),
            panel.view_status.height(), panel.view_status.text(),
        )
        window.main_splitter.setSizes([800, 600])
        app.processEvents()
        assert not panel.view_matches()
        assert rendered_size([595, 842], profile)[1] > profile["height"]
    finally:
        window.close()
        app.processEvents()
