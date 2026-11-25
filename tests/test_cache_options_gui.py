# SPDX-License-Identifier: GPL-3.0-only
"""Ready-only playback choices and read-only cache locations in the real Qt window."""

from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QDesktopServices
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QPushButton

from practice_player.config import Paths, Settings
from practice_player.gui import CacheLocationsDialog, Window
from practice_player.magic_bundle import export_magic
from practice_player.magic_import import import_magic
from practice_player.pipeline import process
from practice_player.transpose import prepare_pitch
from tests.test_gui import wait_until
from tests.test_magic_bundle import portable_study
from tests.test_magic_variants import variant_study


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def dispose(window, app):
    window.close()
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


@pytest.fixture
def cached_window(app, tmp_path, fake_engine):
    request, _, paths = portable_study(tmp_path / "project", kind="pdf", count=4)
    source = Path(request["audio"])
    prepared = process(source, Settings("hyperace"), paths, runner=fake_engine)
    window = Window(paths)
    window.resize(800, 600)
    window.pending_source = source
    window.load_result(prepared.to_dict())
    window.score_panel.open_pdf(Path(request["pdf"]))
    window.score_panel.load_timeline(Path(request["timeline"]))
    window.audio_output.setVolume(0)
    window.show()
    wait_until(app, lambda: window.player.isSeekable())
    yield window
    dispose(window, app)


def test_empty_window_keeps_cache_details_reachable_without_enabling_audio(app, tmp_path):
    window = Window(Paths(tmp_path / "empty"))
    window.resize(800, 560)
    window.show()
    try:
        assert window.playback_variants.count() == 0
        assert not window.playback_variants.isEnabled()
        assert "준비" in window.variant_hint.text()
        QTest.qWait(50)
        window.controller.ensureWidgetVisible(window.details_button)
        QTest.qWait(50)
        logs = QRect(window.logs_toggle.mapTo(window, QPoint()), window.logs_toggle.size())
        details = QRect(window.details_button.mapTo(window, QPoint()), window.details_button.size())
        assert logs.bottom() < details.top()
        assert window.rect().contains(details)
        assert window.childAt(details.center()) is window.details_button
        dialog = CacheLocationsDialog(window)
        assert dialog.findChild(QLineEdit, "cacheRootPath").text() == str(window.paths.processed)
        for prefix in ("audioCache", "scoreCache"):
            field = dialog.findChild(QLineEdit, prefix + "Path")
            assert field.isReadOnly() and not field.text() and field.placeholderText()
            assert not dialog.findChild(QPushButton, prefix + "Copy").isEnabled()
            assert not dialog.findChild(QPushButton, prefix + "Open").isEnabled()
        dialog.deleteLater()
    finally:
        dispose(window, app)


def test_selector_prunes_deleted_files_and_never_offers_preparation_checkboxes(cached_window):
    window = cached_window
    combo = window.playback_variants
    assert {combo.itemData(i) for i in range(combo.count())} == {
        "no_vocals", "vocals", "original",
    }
    window.variant_checks["no_guitar"].setChecked(True)
    combo.showPopup()
    assert combo.findData("no_guitar") == -1
    combo.hidePopup()
    Path(window.prepared_variants["vocals"]["path"]).unlink()
    combo.showPopup()
    assert combo.findData("vocals") == -1
    assert combo.currentData() == "no_vocals"
    combo.hidePopup()
    Path(window.prepared_variants["original"]["path"]).write_bytes(b"")
    combo.showPopup()
    assert combo.count() == 1 and combo.currentData() == "no_vocals"
    assert not combo.isEnabled() and "1개" in window.variant_hint.text()
    assert window.play_button.isEnabled()


def test_deleted_selection_is_rejected_before_starting_work_or_changing_playback(
    cached_window, monkeypatch,
):
    window = cached_window
    window.player.setPosition(1500)
    window.speed.setValue(75)
    before = (window.player.source(), window.player.position(), window.player.playbackRate(),
              window.selected_variant, window.score_panel.timeline)
    jobs = []
    monkeypatch.setattr(window, "start_job", lambda args: jobs.append(args))
    combo = window.playback_variants
    combo.setCurrentIndex(combo.findData("vocals"))
    Path(window.prepared_variants["vocals"]["path"]).unlink()
    combo.activated.emit(combo.currentIndex())
    assert not jobs and "캐시 파일이 없습니다" in window.status.text()
    assert combo.findData("vocals") == -1 and combo.currentData() == "no_vocals"
    assert (window.player.source(), window.player.position(), window.player.playbackRate(),
            window.selected_variant, window.score_panel.timeline) == before


def test_remaining_single_cache_can_replace_a_missing_current_option(cached_window, app):
    window = cached_window
    pitched = prepare_pitch(window.base_instrumental, window.paths, 2)
    window.load_pitch({"path": str(pitched), "semitones": 2})
    wait_until(app, lambda: window.player.isSeekable() and window._pitch_pending is None)
    # The player owns the pitched file, so removing these base files is portable.
    window.base_instrumental.unlink()
    Path(window.prepared_variants["original"]["path"]).unlink()
    combo = window.playback_variants
    combo.showPopup()
    assert combo.isEnabled() and combo.count() == 1 and combo.currentIndex() == -1
    index = combo.model().index(0, 0)
    QTest.mouseClick(combo.view().viewport(), Qt.MouseButton.LeftButton,
                     pos=combo.view().visualRect(index).center())
    wait_until(app, lambda: window.worker is None and window.selected_variant == "vocals")
    assert combo.currentData() == "vocals" and not combo.isEnabled()
    assert window.pitch_semitones == 2 and not window.player.isPlaying()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_details_button_shows_actual_paths_and_copy_open_preserve_practice(
    cached_window, app, monkeypatch, theme,
):
    window = cached_window
    window.theme.setCurrentIndex(window.theme.findData(theme))
    window.player.setPosition(1700)
    window.speed.setValue(75)
    pitched = prepare_pitch(window.base_instrumental, window.paths, 2)
    window._pitch_position = window.player.position()
    window.load_pitch({"path": str(pitched), "semitones": 2})
    wait_until(app, lambda: window.player.isSeekable() and window._pitch_pending is None)
    window.source_name = "<b>Authored study</b>"
    expected = {
        "cacheRoot": window.paths.processed,
        "audioCache": window.base_instrumental,
        "playbackCache": window.output_path,
        "scoreCache": window.score_panel.timeline_path,
    }
    original_bytes = {path: path.read_bytes() for path in expected.values() if path.is_file()}
    before = (window.player.source(), window.player.position(), window.player.playbackRate(),
              window.selected_variant, window.score_panel.timeline)
    opened, errors, inspected = [], [], []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url) or True)

    def inspect():
        dialog = app.activeModalWidget()
        try:
            assert isinstance(dialog, CacheLocationsDialog)
            song = dialog.findChild(QLabel, "cacheCurrentSong")
            assert song.text() == window.source_name
            assert song.textFormat() == Qt.TextFormat.PlainText
            for prefix, path in expected.items():
                field = dialog.findChild(QLineEdit, prefix + "Path")
                assert field.isReadOnly() and field.text() == str(path)
                QTest.mouseClick(dialog.findChild(QPushButton, prefix + "Copy"),
                                 Qt.MouseButton.LeftButton)
                assert app.clipboard().text() == str(path)
                QTest.mouseClick(dialog.findChild(QPushButton, prefix + "Open"),
                                 Qt.MouseButton.LeftButton)
                expected_directory = path if path.is_dir() else path.parent
                assert opened[-1].isLocalFile()
                # Qt uses forward slashes for local Windows URLs, unlike str(Path).
                assert Path(opened[-1].toLocalFile()) == expected_directory
            assert not window.worker and not window.player.isPlaying()
            inspected.append(True)
        except BaseException as error:
            errors.append(error)
        finally:
            if dialog:
                dialog.reject()

    window.controller.ensureWidgetVisible(window.details_button)
    QTimer.singleShot(80, inspect)
    QTest.mouseClick(window.details_button, Qt.MouseButton.LeftButton)
    assert not errors, errors
    assert inspected and len(opened) == 4
    assert all(path.read_bytes() == content for path, content in original_bytes.items())
    assert (window.player.source(), window.player.position(), window.player.playbackRate(),
            window.selected_variant, window.score_panel.timeline) == before


def test_imported_archive_details_follow_actual_audio_and_selected_score_part(app, tmp_path):
    request, _, _ = variant_study(tmp_path / "producer", kind="gp", pitch=2)
    request["audio_format"] = "wav"
    archive = export_magic(request)
    paths = Paths(tmp_path / "reader")
    paths.create()
    imported = import_magic(archive, paths)
    window = Window(paths)
    window._pending_magic = archive
    window.load_imported(imported)
    try:
        assert window.imported_bundle and window.playback_variants.count() == 2
        panel = window.score_panel
        for index in range(panel.parts.count()):
            panel.parts.setCurrentIndex(index)
            panel.change_part(index)
            dialog = CacheLocationsDialog(window)
            assert dialog.findChild(QLineEdit, "audioCachePath").text() == str(
                window.base_instrumental
            )
            score = Path(dialog.findChild(QLineEdit, "scoreCachePath").text())
            assert score == panel.timeline_path and score.is_file()
            assert paths.processed / "imported-magic" in score.parents
            assert dialog.findChild(QLineEdit, "playbackCachePath") is None
            dialog.deleteLater()
    finally:
        dispose(window, app)
