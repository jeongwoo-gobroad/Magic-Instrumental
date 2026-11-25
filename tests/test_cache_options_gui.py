# SPDX-License-Identifier: GPL-3.0-only
"""Cached playback choices and editable cache folders in the real Qt window."""

import shutil
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QDesktopServices
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog, QLabel, QLineEdit, QPushButton, QScrollArea

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
        for prefix in ("audioCacheFolder", "scoreCacheFolder"):
            field = dialog.findChild(QLineEdit, prefix + "Path")
            assert field.text() == str(window.paths.processed) and not field.isReadOnly()
            assert dialog.findChild(QPushButton, prefix + "Browse").isEnabled()
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
    assert window.playback_cache_count.text() == "캐시 3개"
    assert window.playback_cache_count.parent() is window.playback_choice
    window.options_toggle.setChecked(False)
    assert window.playback_choice.isVisible() and combo.isVisible()
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
    assert window.playback_cache_count.text() == "캐시 1개"
    assert window.play_button.isEnabled()


def test_nonempty_truncated_cache_is_not_offered_in_the_actual_dropdown(cached_window):
    window = cached_window
    item = window.prepared_variants["vocals"]
    Path(item["path"]).write_bytes(b"not a complete cached audio file")
    window.playback_variants.showPopup()
    try:
        assert window.playback_variants.findData("vocals") == -1
        assert window.playback_cache_count.text() == "캐시 2개"
        assert window.playback_variants.currentData() == "no_vocals"
    finally:
        window.playback_variants.hidePopup()


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
            dialog.findChild(QPushButton, "currentCacheFilesToggle").click()
            song = dialog.findChild(QLabel, "cacheCurrentSong")
            assert song.text() == window.source_name
            assert song.textFormat() == Qt.TextFormat.PlainText
            for prefix, path in expected.items():
                field = dialog.findChild(QLineEdit, prefix + "Path")
                assert field.isReadOnly() and field.text() == str(path)
                dialog.findChild(QScrollArea).ensureWidgetVisible(field)
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
    assert inspected and len(opened) == 3
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


def test_choose_and_save_cache_folders_preserves_open_practice_and_survives_restart(
    cached_window, app, tmp_path, monkeypatch,
):
    window = cached_window
    window.player.setPosition(1300)
    window.speed.setValue(75)
    window.theme.setCurrentIndex(window.theme.findData("light"))
    audio_folder = tmp_path / "다른 위치" / "음원"
    score_folder = tmp_path / "다른 위치" / "악보 분석"
    before = (window.player.source(), window.player.position(), window.player.playbackRate(),
              window.selected_variant, window.score_panel.timeline, window.base_instrumental)
    existing = {path: path.read_bytes() for path in (
        window.base_instrumental, window.score_panel.timeline_path,
    )}
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *_: str(audio_folder))
    dialog = CacheLocationsDialog(window)
    dialog.show()
    dialog.findChild(QPushButton, "audioCacheFolderBrowse").click()
    dialog.findChild(QLineEdit, "scoreCacheFolderPath").setText(str(score_folder))
    assert dialog.save_button.isEnabled()
    QTest.mouseClick(dialog.save_button, Qt.MouseButton.LeftButton)
    assert not dialog.isVisible()
    assert window.paths.audio_cache == audio_folder.resolve()
    assert window.paths.score_cache == score_folder.resolve()
    assert window.score_panel.paths is window.paths
    assert (
        window.player.source(), window.player.position(), window.player.playbackRate(),
        window.selected_variant, window.score_panel.timeline, window.base_instrumental,
    ) == before
    assert all(path.read_bytes() == content for path, content in existing.items())
    assert not list(audio_folder.iterdir()) and not list(score_folder.iterdir())
    window.preferences.sync()
    restarted = Window(Paths(window.paths.root))
    try:
        assert restarted.paths.audio_cache == audio_folder.resolve()
        assert restarted.paths.score_cache == score_folder.resolve()
        assert restarted.preferences.fileName() == window.preferences.fileName()
        assert restarted.theme.currentData() == "light"
    finally:
        dispose(restarted, app)
        dialog.deleteLater()


def test_cancel_and_invalid_destination_leave_both_cache_locations_unchanged(
    cached_window, tmp_path,
):
    window = cached_window
    before = window.paths
    settings = before.processed / "cache-locations.json"
    dialog = CacheLocationsDialog(window)
    dialog.fields["audioCacheFolder"].setText(str(tmp_path / "cancelled-folder"))
    dialog.reject()
    assert window.paths is before and not settings.exists()
    assert not (tmp_path / "cancelled-folder").exists()
    dialog.deleteLater()
    invalid = tmp_path / "a-file.txt"
    invalid.write_text("Keep this file", encoding="utf-8")
    dialog = CacheLocationsDialog(window)
    dialog.show()
    dialog.fields["audioCacheFolder"].setText(str(tmp_path / "new-audio"))
    dialog.fields["scoreCacheFolder"].setText(str(invalid))
    dialog.save_button.click()
    assert dialog.isVisible() and "저장하지 못했습니다" in dialog.notice.text()
    assert window.paths is before and window.score_panel.paths is before
    assert not settings.exists() and invalid.read_text(encoding="utf-8") == "Keep this file"
    dialog.reject()
    dialog.deleteLater()


def test_new_cache_folder_is_used_by_the_next_real_worker_and_default_can_be_restored(
    cached_window, app, tmp_path,
):
    window = cached_window
    original = window.paths.processed
    previous_audio = window.base_instrumental
    audio = tmp_path / "relocated audio"
    score = tmp_path / "relocated scores"
    window.apply_cache_locations(str(audio), str(score))
    # Only authored fixture data is copied; changing the preference never moves files.
    shutil.copytree(original / window.loaded_source_hash, audio / window.loaded_source_hash)
    window.load_cached_audio()
    wait_until(app, lambda: (
        window.worker is None and window.base_instrumental.is_relative_to(audio)
    ))
    assert previous_audio.is_file()
    assert all(Path(item["path"]).is_relative_to(audio)
               for item in window.prepared_variants.values())
    assert not window.player.isPlaying()
    dialog = CacheLocationsDialog(window)
    dialog.defaults.click()
    assert window.paths.audio_cache == audio
    dialog.save_button.click()
    assert window.paths.audio_cache == original and window.paths.score_cache == original
    assert window.base_instrumental.is_relative_to(audio)
    assert Paths(window.paths.root).audio_cache == original
    dialog.deleteLater()


def test_corrupt_settings_are_repairable_from_details_without_silent_cache_work(app, tmp_path):
    paths = Paths(tmp_path)
    paths.create()
    (paths.processed / "cache-locations.json").write_text("broken", encoding="utf-8")
    window = Window(Paths(tmp_path))
    try:
        assert window.paths.cache_location_error
        assert "상세 옵션" in window.status.text()
        window.start_job(["cached-audio", "--input", "missing.wav", "--events"])
        assert window.worker is None
        dialog = CacheLocationsDialog(window)
        assert dialog.save_button.isEnabled()
        dialog.save_button.click()
        assert not window.paths.cache_location_error
        assert not Paths(tmp_path).cache_location_error
        dialog.deleteLater()
    finally:
        dispose(window, app)


def test_changed_cache_settings_cannot_leave_a_pending_variant_or_pause_playback(
    cached_window, app,
):
    window = cached_window
    window.player.play()
    wait_until(app, window.player.isPlaying)
    before = (window.player.source(), window.selected_variant, window.score_panel.timeline)
    (window.paths.processed / "cache-locations.json").write_text("changed", encoding="utf-8")
    window.begin_variant_switch("vocals")
    assert window.worker is None
    assert window._pending_variant is None and window._pending_catalog is None
    assert window._variant_restore is None and window._prepare_restore is None
    assert window.player.isPlaying()
    assert (window.player.source(), window.selected_variant, window.score_panel.timeline) == before
    assert "저장 위치" in window.status.text()
    window.player.pause()


def test_details_refreshes_actual_paths_after_a_worker_finishes(cached_window, app):
    window = cached_window
    window.begin_variant_switch("vocals")
    assert window.worker is not None
    dialog = CacheLocationsDialog(window)
    dialog.show()
    assert not dialog.fields["audioCacheFolder"].isEnabled()
    wait_until(app, lambda: (
        window.worker is None and window.selected_variant == "vocals"
        and dialog.fields["audioCacheFolder"].isEnabled()
    ))
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert dialog.findChild(QLineEdit, "audioCachePath").text() == str(window.base_instrumental)
    assert not dialog.refresh_timer.isActive()
    dialog.reject()
    dialog.deleteLater()
