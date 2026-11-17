# SPDX-License-Identifier: GPL-3.0-only
from copy import deepcopy

from pypdf import PdfWriter
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import QApplication

from practice_player.cache import hash_file
from practice_player.config import Paths
from practice_player.gui import Window
from practice_player.score import ScoreTimeline
from tests.test_gui import wait_until


def performed_score():
    # A repeat returns from printed measure 3 to 1, then skips to ending 4.
    labels = [1, 2, 3, 1, 2, 4]
    starts = [0, 0.5, 1.75, 3, 4.5, 6.25]
    cues = [
        {
            "start": start,
            "source_index": label - 1,
            "measure": str(label),
            "visit": 2 if index in (3, 4) else 1,
            "page": 0 if label < 3 else 1,
            "marks": [],
        }
        for index, (label, start) in enumerate(zip(labels, starts))
    ]
    return ScoreTimeline({"version": 1, "page_count": 2, "cues": cues, "offset": 0.125, "end": 7.5})


def test_measure_steps_follow_performance_order_and_variable_durations():
    plan = performed_score()
    assert plan.measure_target(2.7, 1, 10) == (3, 3125)  # printed 3 -> printed 1, visit 2
    assert plan.measure_target(3.5, -2, 10) == (1, 625)
    assert plan.measure_target(0.8, 2, 10) == (3, 3125)
    assert plan.measure_target(4.9, 1, 10) == (5, 6375)  # second ending
    assert plan.measure_target(3.125, -1, 10) == (2, 1875)


def test_measure_seek_intro_outro_clamps_and_partial_audio():
    plan = performed_score()
    assert plan.measure_target(0, 1, 10) == (0, 125)
    assert plan.measure_target(0, 3, 10) == (2, 1875)
    assert plan.measure_target(0, -10, 10) == (0, 125)
    assert plan.measure_target(9, -1, 10) == (5, 6375)
    assert plan.measure_target(9, -3, 10) == (3, 3125)
    assert plan.measure_target(6.8, 10000, 10) == (5, 6375)
    assert plan.measure_target(2.7, 10000, 4) == (3, 3125)
    assert plan.measure_target(0, 1, 0) is None
    data = deepcopy(plan.data)
    data["offset"] = -3.25
    trimmed = ScoreTimeline(data)
    assert trimmed.measure_target(0.1, -100, 5) == (3, 0)
    assert trimmed.measure_target(0.1, 1, 5) == (4, 1250)
    data["offset"] = -20
    assert ScoreTimeline(data).measure_target(0, 1, 10) is None


def test_repeated_clicks_do_not_stick_before_fractional_boundaries():
    data = deepcopy(performed_score().data)
    data["cues"] = data["cues"][:3]
    for cue, start in zip(data["cues"], [0, 0.5001, 1.0001]):
        cue["start"] = start
    data.update(offset=0.0001, end=2)
    plan = ScoreTimeline(data)
    first = plan.measure_target(0.2, 1, 3)
    second = plan.measure_target(first[1] / 1000, 1, 3)
    assert first == (1, 501)
    assert second == (2, 1001)


def test_gui_measure_buttons_keep_transport_and_sync_dependents(tmp_path, tone):
    app = QApplication.instance() or QApplication([])
    paths = Paths(tmp_path / "project")
    paths.create()
    pdf = paths.score_targets / f"{tone.stem}.pdf"
    writer = PdfWriter()
    for _ in range(2):
        writer.add_blank_page(width=595, height=842)
    with pdf.open("wb") as output:
        writer.write(output)
    window = Window(paths)
    window.show()
    try:
        assert not window.previous_measure.isEnabled()
        assert not window.next_measure.isEnabled()
        window.pending_source = tone
        window.source_name = tone.name
        window.load_result(
            {
                "instrumental": str(tone),
                "source_hash": hash_file(tone),
                "cached": True,
                "device": "cpu",
            }
        )
        window.audio_output.setVolume(0)
        window.metronome.mute.setChecked(True)
        wait_until(app, lambda: window.player.duration() > 0 and window.player.isSeekable())
        assert not window.next_measure.isEnabled()  # PDF alone is insufficient
        data = deepcopy(performed_score().data)
        for index, cue in enumerate(data["cues"]):
            cue["start"] = index / 2
        data.update(end=2.75, audio_sha256=hash_file(tone), pdf_sha256=hash_file(pdf))
        timeline = tmp_path / "timeline.json"
        ScoreTimeline(data).save(timeline)
        window.score_panel.load_timeline(timeline)
        assert window.next_measure.isEnabled()
        window.metronome.linked.setChecked(True)
        window.speed.setValue(50)
        window.player.play()
        wait_until(
            app, lambda: window.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        )
        window.player.pause()
        window.player.setPosition(1750)
        window.measure_step.setValue(2)
        window.previous_measure.click()
        assert window.player.position() == 625
        assert window.player.playbackState() == QMediaPlayer.PlaybackState.PausedState
        assert window.metronome.anchor_position == 0.625
        assert window.score_panel.current_index == 1
        window.next_measure.click()
        assert window.player.position() == 1625
        assert "2회차" in window.measure_hint.text()
        window.player.play()
        wait_until(
            app, lambda: window.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        )
        window.next_measure.click()
        assert window.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        assert window.player.playbackRate() == 0.5
        assert window.player.position() >= 2625
        assert window.score_panel.view.page == 1
        window.player.pause()
        window.set_busy(True)
        assert not window.previous_measure.isEnabled()
        window.set_busy(False)
        assert window.previous_measure.isEnabled()
        window.measure_step.setValue(4)
        window.score_panel.open_pdf(pdf)  # no cached schedule for this PDF yet
        assert not window.next_measure.isEnabled()
    finally:
        window.close()
        app.processEvents()
    reopened = Window(paths)
    try:
        assert reopened.measure_step.value() == 4
    finally:
        reopened.close()
        app.processEvents()
