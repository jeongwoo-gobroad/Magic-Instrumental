# SPDX-License-Identifier: GPL-3.0-only
"""Exercise the desktop controls with real workers, Qt transport and exported files."""

import json
import shutil
import zipfile

import soundfile as sf
from PySide6.QtCore import QCoreApplication, QEvent, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QComboBox, QDialogButtonBox, QFileDialog

from practice_player.config import Paths, Settings
from practice_player.gui import Window
from practice_player.pipeline import process
from tests.test_gui import wait_until
from tests.test_guitar_pro import gp_song


def test_first_track_to_practice_bundle_and_reopen(tone, tmp_path, fake_engine, monkeypatch):
    app = QApplication.instance() or QApplication([])
    paths = Paths(tmp_path / "연습 공간")
    paths.create()
    window = Window(paths)
    window.show()

    def click(widget):
        assert widget.isEnabled(), widget.text()
        QTest.mouseClick(widget, Qt.MouseButton.LeftButton)

    def finish():
        wait_until(app, lambda: window.worker is None, timeout=45)
        assert not window.job_error, window.logs.toPlainText()

    try:
        click(window.prepare_button)
        assert "새로고침" in window.status.text()
        source = paths.targets / tone.name
        shutil.copyfile(tone, source)
        process(source, Settings(), paths, runner=fake_engine)
        click(window.refresh_button)
        assert window.tracks.currentItem().text() == tone.name
        click(window.prepare_button)
        finish()
        wait_until(app, lambda: window.player.isSeekable())
        assert window.key_apply.isEnabled()
        window.audio_output.setVolume(0)
        window.speed.setValue(75)
        click(window.play_button)
        wait_until(app, lambda: window.player.position() > 150)
        click(window.play_button)
        position = window.player.position()
        window.key.setValue(2)
        click(window.key_apply)
        finish()
        wait_until(app, lambda: window.player.isSeekable() and window._pitch_pending is None)
        assert window.pitch_semitones == 2 and abs(window.player.position() - position) < 50
        assert window.player.playbackRate() == 0.75

        monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_: (str(source), "WAV"))
        click(window.export_button)
        assert window.worker is None and "원본 음원" in window.status.text()
        assert source.read_bytes() == tone.read_bytes()

        export = tmp_path / "연습 반주.flac"
        monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_: (str(export), "FLAC"))
        click(window.export_button)
        finish()
        assert abs(sf.info(export).duration - 4) < 0.15

        score = gp_song(paths.score_targets / "악보.gp5", count=4)
        monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_: (str(score), "GP"))
        panel = window.score_panel
        click(panel.choose)
        finish()
        assert panel.gp_source == score and panel.view.document

        def choose_tempo():
            dialog = app.activeModalWidget()
            mode = next(c for c in dialog.findChildren(QComboBox) if c.findData("tempo") >= 0)
            mode.setCurrentIndex(mode.findData("tempo"))
            dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()

        QTimer.singleShot(0, choose_tempo)
        click(panel.analyze)
        finish()
        assert panel.timeline and len(panel.timeline.cues) == 4
        click(window.next_measure)
        assert window.player.position() >= 2000
        click(window.previous_measure)
        assert window.player.position() == 0
        click(window.region.enabled)
        assert window.region.bounds()
        click(window.region.enabled)

        bundle = tmp_path / "연습 세트.magic"
        window.metronome.origin.setValue(-0.25)
        monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_: (str(bundle), "Magic"))
        click(window.magic_button)
        assert not window.magic_button.isEnabled()
        finish()
        with zipfile.ZipFile(bundle) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            assert manifest["audio"]["pitch_semitones"] == 2
            assert manifest["practice"]["origin"] == -0.25
            assert abs(manifest["audio"]["duration"] - 3) < 0.01
            assert archive.read("score/source.gp5") == score.read_bytes()
        window.theme.setCurrentIndex(window.theme.findData("light"))
    finally:
        window.close()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    reopened = Window(paths)
    reopened.show()
    try:
        assert reopened.theme.currentData() == "light"
        click(reopened.prepare_button)
        wait_until(app, lambda: reopened.worker is None and bool(reopened.score_panel.timeline))
        assert reopened.score_panel.gp_source == score
        assert reopened.score_panel.current_index in (None, 0)
        # A failed GP worker for another recording must leave no usable old timeline.
        other = paths.targets / "두 번째.wav"
        shutil.copyfile(tone, other)
        with other.open("ab") as stream:
            stream.write(b"fixture")
        process(other, Settings(), paths, runner=fake_engine)
        (paths.score_targets / "두 번째.gp5").write_bytes(b"bad score")
        click(reopened.refresh_button)
        matches = reopened.tracks.findItems(other.name, Qt.MatchFlag.MatchExactly)
        reopened.tracks.setCurrentItem(matches[0])
        click(reopened.prepare_button)
        wait_until(app, lambda: reopened.worker is None and bool(reopened.job_error))
        assert not reopened.score_panel.timeline and not reopened.score_panel.view.document
        assert not reopened.magic_button.isEnabled() and not reopened.next_measure.isEnabled()
    finally:
        reopened.close()
        reopened.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
