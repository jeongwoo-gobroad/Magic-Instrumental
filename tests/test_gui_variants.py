# SPDX-License-Identifier: GPL-3.0-only
"""Exercise cached listening choices and imported score pages with real Qt workers."""

import json
import zipfile
from pathlib import Path

import pytest
import soundfile as sf
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QRect, Qt, QTimer, QUrl
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialogButtonBox,
    QFileDialog,
    QListWidget,
    QMessageBox,
)

from practice_player.cache import hash_file
from practice_player.combinations import process_combination
from practice_player.config import Paths, Settings
from practice_player.gui import Window
from practice_player.magic_bundle import export_magic
from practice_player.magic_import import import_magic, load_imported
from practice_player.page_images import PageImages
from practice_player.pipeline import process
from practice_player.score import ScoreTimeline
from practice_player.variants import SUPPORTED_VARIANTS, canonical_combination
from tests.test_audio_combinations import SequentialTestEngine
from tests.test_audio_variants import KeysSynthEngine, SixStemEngine
from tests.test_gui import wait_until
from tests.test_magic_bundle import portable_study
from tests.test_magic_export_gui import schedule_magic_quality
from tests.test_magic_variants import variant_study
from tests.test_score_follow_resume import assert_current_measure_visible


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def dispose(window, app):
    if window.worker:
        window.cancel_job()
        wait_until(app, lambda: window.worker is None)
    window.close()
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def finish(window, app, *, error=False):
    wait_until(app, lambda: window.worker is None, timeout=45)
    assert bool(window.job_error) == error, window.logs.toPlainText()
    wait_until(app, lambda: (
        window.player.isSeekable() and window._pitch_pending is None
        and window._pending_variant is None and window._variant_restore is None
    ))


def select_option(combo, identifier):
    index = combo.findData(identifier)
    assert index >= 0 and combo.isEnabled()
    combo.showPopup()
    model_index = combo.model().index(index, 0)
    combo.view().scrollTo(model_index)
    QTest.mouseClick(combo.view().viewport(), Qt.MouseButton.LeftButton,
                     pos=combo.view().visualRect(model_index).center())


def prepare_variant(window, app, identifier):
    window.models.setCurrentIndex(window.models.findData("auto"))
    window.select_preparation_variants(False)
    window.variant_checks[identifier].setChecked(True)
    QTest.mouseClick(window.prepare_button, Qt.MouseButton.LeftButton)
    finish(window, app)
    assert window.selected_variant == identifier


def seed_combination(window, identifier):
    engine = SequentialTestEngine()

    def synthetic_process(source, settings, paths, report, **options):
        return process(source, settings, paths, report, stem_runner=engine, **options)

    return process_combination(
        window.source_path, identifier, "auto", "cpu", False, window.paths,
        processor=synthetic_process,
    )


def practice_state(window, app, *, key=2):
    window.speed.setValue(75)
    if window.pitch_semitones != key:
        window.key.setValue(key)
        QTest.mouseClick(window.key_apply, Qt.MouseButton.LeftButton)
        finish(window, app)
    window.region.first.setCurrentIndex(window.region.first.findData(1))
    window.region.last.setCurrentIndex(window.region.last.findData(2))
    window.region.enabled.setChecked(True)
    window.player.setPosition(3500)
    QTest.qWait(100)
    assert window.region.bounds() == (2000, 6000)
    return window.player.position()


def assert_practice_state(window, position, *, key=2):
    assert not window.player.isPlaying()
    assert abs(window.player.position() - position) < 50
    assert window.pitch_semitones == window.key.value() == key
    assert window.speed.value() == 75 and window.player.playbackRate() == .75
    assert window.region.enabled.isChecked()
    assert (window.region.first.currentData(), window.region.last.currentData()) == (1, 2)
    assert window.region.bounds() == (2000, 6000)


@pytest.fixture
def native_window(app, tmp_path, fake_engine):
    request, _, paths = portable_study(tmp_path / "project", kind="pdf", count=4)
    source = Path(request["audio"])
    process(source, Settings("hyperace"), paths, runner=fake_engine)
    process(source, Settings("leap"), paths, runner=fake_engine)
    process(source, Settings("sw6"), paths, stem_runner=SixStemEngine(), prepare_all=True)
    process(source, Settings("mega"), paths, stem_runner=KeysSynthEngine(),
            variant_id="keys", prepare_all=True)
    window = Window(paths)
    window.resize(1200, 900)
    window.show()
    window.audio_output.setVolume(0)
    window.models.setCurrentIndex(window.models.findData("hyperace"))
    window.devices.setCurrentIndex(window.devices.findData("cpu"))
    try:
        QTest.mouseClick(window.prepare_button, Qt.MouseButton.LeftButton)
        finish(window, app)
        window.score_panel.open_pdf(Path(request["pdf"]))
        timeline = Path(request["timeline"])
        reviewed = ScoreTimeline.load(timeline)
        reviewed.data["reviewed"] = True
        reviewed.save(timeline)
        window.score_panel.load_timeline(timeline)
        wait_until(app, lambda: window.region.first.count() == 4)
        yield window
    finally:
        dispose(window, app)


@pytest.mark.parametrize("all_variants", [False, True])
def test_preparing_new_same_song_variant_keeps_key_speed_position_and_loop(
    native_window, app, monkeypatch, all_variants,
):
    window = native_window
    position = practice_state(window, app)
    timeline = window.score_panel.timeline
    timeline_bytes = window.score_panel.timeline_path.read_bytes()
    original = window.source_path.read_bytes()
    source = window.source_path
    jobs, start_job = [], window.start_job

    def record(args):
        jobs.append(args)
        start_job(args)

    monkeypatch.setattr(window, "start_job", record)
    expected = canonical_combination(SUPPORTED_VARIANTS) if all_variants else "no_guitar"
    if all_variants:
        seed_combination(window, expected)
        window.models.setCurrentIndex(window.models.findData("auto"))
        QTest.mouseClick(window.select_all_variants, Qt.MouseButton.LeftButton)
        QTest.mouseClick(window.prepare_button, Qt.MouseButton.LeftButton)
        finish(window, app)
        assert set(window.prepared_variants) == {*SUPPORTED_VARIANTS, expected}
        start, end = jobs[0].index("--variants") + 1, jobs[0].index("--device")
        assert tuple(jobs[0][start:end]) == (*SUPPORTED_VARIANTS, expected)
        assert jobs[0][jobs[0].index("--model") + 1] == "auto"
    else:
        prepare_variant(window, app, "no_guitar")
    assert window.selected_variant == expected
    assert [job[0] for job in jobs] == ["separate", "pitch"]
    assert "--expected-sha256" in jobs[1] and "--expected-frames" in jobs[1]
    assert window.job_result["type"] == "pitch_result"
    assert window.base_instrumental == Path(window.prepared_variants[expected]["path"])
    assert {"no_vocals", "guitar", "no_guitar", "drums", "no_drums"} <= (
        window.prepared_variants.keys()
    )
    assert window.score_panel.timeline is timeline
    assert window.score_panel.timeline_path.read_bytes() == timeline_bytes
    assert window.source_path == source and source.read_bytes() == original
    assert_practice_state(window, position)

    select_option(window.playback_variants, "guitar")
    finish(window, app)
    assert window.selected_variant == "guitar"
    assert [job[0] for job in jobs] == ["separate", "pitch", "pitch"]
    assert window.score_panel.timeline is timeline
    assert_practice_state(window, position)

    if all_variants:
        for identifier in ("keys", "no_keys", "piano", "synth"):
            select_option(window.playback_variants, identifier)
            finish(window, app)
            assert window.selected_variant == identifier
            assert window.score_panel.timeline is timeline
            assert_practice_state(window, position)


@pytest.mark.parametrize("failure", ["corrupt-option", "unsupported-model"])
def test_failed_option_preparation_retains_the_loaded_song_and_practice_range(
    native_window, app, failure,
):
    window = native_window
    prepare_variant(window, app, "no_guitar")
    position = practice_state(window, app)
    before = (window.output_path, window.base_instrumental, window.selected_variant,
              window.player.source(), window.score_panel.timeline)
    if failure == "corrupt-option":
        other = Path(window.prepared_variants["guitar"]["path"])
        # Keep the size unchanged so the worker, rather than the popup, catches corruption.
        with other.open("r+b") as stream:
            first = stream.read(1)
            stream.seek(0)
            stream.write(bytes([first[0] ^ 1]))
        select_option(window.playback_variants, "guitar")
    else:
        window.models.setCurrentIndex(window.models.findData("hyperace"))
        window.select_preparation_variants(False)
        window.variant_checks["guitar"].setChecked(True)
        QTest.mouseClick(window.prepare_button, Qt.MouseButton.LeftButton)
    finish(window, app, error=True)
    assert (window.output_path, window.base_instrumental, window.selected_variant,
            window.player.source(), window.score_panel.timeline) == before
    assert window.playback_variants.currentData() == before[2]
    assert window.play_button.isEnabled() and window.playback_variants.isEnabled()
    assert_practice_state(window, position)


def test_renamed_original_reuses_same_song_state_and_exports_from_current_path(
    native_window, app, tmp_path, monkeypatch,
):
    window = native_window
    position = practice_state(window, app)
    source, timeline = window.source_path, window.score_panel.timeline
    source_hash, timeline_bytes = hash_file(source), window.score_panel.timeline_path.read_bytes()
    renamed = source.with_name("renamed-study.wav")
    source.rename(renamed)
    QTest.mouseClick(window.refresh_button, Qt.MouseButton.LeftButton)
    assert window.tracks.currentItem().data(Qt.ItemDataRole.UserRole) == str(renamed)
    prepare_variant(window, app, "no_guitar")
    assert window.source_path == window.score_panel.audio == renamed
    assert window.source_name == window.current_track.text() == renamed.name
    assert window.loaded_source_hash == source_hash and hash_file(renamed) == source_hash
    assert window.score_panel.timeline is timeline
    assert window.score_panel.timeline_path.read_bytes() == timeline_bytes
    assert_practice_state(window, position)

    output = tmp_path / "renamed-song.magic"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_: (str(output), "Magic"))
    quality = schedule_magic_quality(app, "wav")
    QTest.mouseClick(window.magic_button, Qt.MouseButton.LeftButton)
    assert not quality["errors"]
    finish(window, app)
    with zipfile.ZipFile(output) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    assert manifest["audio"]["source_sha256"] == source_hash
    assert manifest["title"] == renamed.stem
    assert not source.exists() and hash_file(renamed) == source_hash


def test_export_dialog_requires_a_selection_and_sends_subset_and_default(
    native_window, app, tmp_path, monkeypatch,
):
    window = native_window
    prepare_variant(window, app, "no_guitar")
    position = practice_state(window, app)
    messages, jobs, errors = [], [], []
    start_job = window.start_job
    monkeypatch.setattr(QMessageBox, "information", lambda *args: messages.append(args))
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        lambda *_: (str(tmp_path / "selected.magic"), "Magic"))
    monkeypatch.setattr(window, "start_job", jobs.append)

    def choose():
        dialog = app.activeModalWidget()
        try:
            quality = dialog.findChild(QComboBox, "magicAudioQuality")
            choices = dialog.findChild(QListWidget, "magicAudioVariants")
            first = dialog.findChild(QComboBox, "magicDefaultVariant")
            assert quality.currentData() == "mp3:256"
            assert first.currentData() == "no_guitar"
            assert choices.count() == len(window.prepared_variants)
            for index in range(choices.count()):
                choices.item(index).setCheckState(Qt.CheckState.Unchecked)
            save = dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Save)
            QTest.mouseClick(save, Qt.MouseButton.LeftButton)
            assert dialog.isVisible() and first.count() == 0 and len(messages) == 1
            for index in range(choices.count()):
                item = choices.item(index)
                if item.data(Qt.ItemDataRole.UserRole) in {"vocals", "guitar"}:
                    item.setCheckState(Qt.CheckState.Checked)
            assert {first.itemData(index) for index in range(first.count())} == {"vocals", "guitar"}
            first.setCurrentIndex(first.findData("guitar"))
            QTest.mouseClick(save, Qt.MouseButton.LeftButton)
        except Exception as error:
            errors.append(str(error))
            dialog.reject()

    QTimer.singleShot(0, choose)
    QTest.mouseClick(window.magic_button, Qt.MouseButton.LeftButton)
    assert not errors
    request_path = window.paths.processed / "magic-export-request.json"
    request = json.loads(request_path.read_text(encoding="utf-8"))
    assert jobs == [["magic-export", "--request", str(request_path), "--events"]]
    assert request["default_audio_variant"] == "guitar"
    assert [item["id"] for item in request["audio_variants"]] == ["vocals", "guitar"]
    assert all(item["path"] == window.prepared_variants[item["id"]]["path"]
               for item in request["audio_variants"])
    for item in request["audio_variants"]:
        prepared = window.prepared_variants[item["id"]]
        assert item["expected_sha256"] == prepared["sha256"]
        assert item["expected_source_sha256"] == prepared["source_hash"]
        assert item["expected_frames"] == prepared["frames"]
    assert request["pitch_semitones"] == 2
    assert request["audio_format"] == "mp3" and request["mp3_bitrate_kbps"] == 256
    assert_practice_state(window, position)
    start_job(jobs[0])
    finish(window, app)
    with zipfile.ZipFile(request["output"]) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["version"] == 4 and manifest["default_audio_variant"] == "guitar"
        assert [item["id"] for item in manifest["audio_variants"]] == ["vocals", "guitar"]
        assert all(item["mode"] == "stem" and item["audio"]["pitch_semitones"] == 2
                   for item in manifest["audio_variants"])
        assert b"expected_" not in archive.read("manifest.json")
    imported_paths = Paths(tmp_path / "second-pc")
    imported_paths.create()
    imported = import_magic(Path(request["output"]), imported_paths)
    bundle = load_imported(imported["bundle_path"])
    assert bundle["default_audio_variant"] == "guitar"
    assert bundle["original_source_sha256"] == window.loaded_source_hash
    assert all(
        item["mode"] == "stem" and item["pitch_semitones"] == 2
        and item["frames"] == 8 * 44100 and item["sample_rate"] == 44100
        and item["source_sha256"] == window.loaded_source_hash
        for item in bundle["audio_variants"]
    )
    cached = import_magic(Path(request["output"]), imported_paths)
    assert cached == {**imported, "cached": True}


def test_stale_cached_variant_export_fails_without_replacing_an_existing_bundle(
    native_window, app, tmp_path, monkeypatch,
):
    window = native_window
    prepare_variant(window, app, "no_guitar")
    guitar = Path(window.prepared_variants["guitar"]["path"])
    samples, rate = sf.read(guitar, dtype="float32", always_2d=True)
    sf.write(guitar, samples * .5, rate, subtype="FLOAT")
    assert sf.info(guitar).frames == window.prepared_variants["guitar"]["frames"]
    assert hash_file(guitar) != window.prepared_variants["guitar"]["sha256"]
    source_hash, current_source = hash_file(window.source_path), window.player.source()
    output = tmp_path / "existing.magic"
    previous = b"previous user practice bundle"
    output.write_bytes(previous)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_: (str(output), "Magic"))
    monkeypatch.setattr(QMessageBox, "question", lambda *_: QMessageBox.StandardButton.Yes)
    quality = schedule_magic_quality(app, "wav")
    QTest.mouseClick(window.magic_button, Qt.MouseButton.LeftButton)
    assert not quality["errors"]
    finish(window, app, error=True)
    assert window.job_result is None
    assert output.read_bytes() == previous
    assert not list(output.parent.glob(".magic-*"))
    assert hash_file(window.source_path) == source_hash
    assert window.player.source() == current_source and window.selected_variant == "no_guitar"


def test_imported_gp_uses_saved_pages_and_preserves_loop_across_part_and_audio_changes(
    app, tmp_path, monkeypatch,
):
    request, _, _ = variant_study(tmp_path / "producer", kind="gp", pitch=2)
    archive = export_magic(request)
    original_archive = hash_file(archive)
    window = Window(Paths(tmp_path / "reader"))
    window.resize(1200, 900)
    window.show()
    window.audio_output.setVolume(0)
    jobs, start_job = [], window.start_job

    def record(args):
        jobs.append(args)
        start_job(args)

    monkeypatch.setattr(window, "start_job", record)
    try:
        window.import_magic(archive)
        finish(window, app)
        panel = window.score_panel
        assert isinstance(panel.view.document, PageImages)
        assert panel.pdf is None and panel.audio is None and panel.gp_source is None
        assert not panel.analyze.isEnabled() and not panel.review.isEnabled()
        assert not window.magic_button.isEnabled()
        assert window.base_pitch_semitones == 2 and window.key.value() == 0
        assert set(window.prepared_variants) == {"no_vocals", "guitar"}
        assert panel.parts.count() == 2 and panel.parts.currentData() == 2
        panel.zoom.setCurrentIndex(panel.zoom.findData("page"))
        position = practice_state(window, app, key=0)
        QTest.qWait(150)
        assert panel.current_index == 1
        assert panel.highlight.isChecked() and panel.follow.isChecked()
        assert not panel.view.label.pixmap().isNull()
        assert not panel.view.label.highlight_rect().isEmpty()
        assert panel.view.label.height() <= panel.view.viewport().height() + 1
        assert_current_measure_visible(panel)
        signature = window.region.signature
        plan_bytes = {Path(part["timeline"]): Path(part["timeline"]).read_bytes()
                      for part in window.imported_bundle["parts"]}
        select_option(panel.parts, 1)
        QTest.qWait(150)
        assert panel.parts.currentData() == 1 and panel.current_index == 1
        assert window.region.signature == signature
        assert_current_measure_visible(panel)
        assert_practice_state(window, position, key=0)
        assert [job[0] for job in jobs] == ["magic-import"]

        select_option(window.playback_variants, "guitar")
        finish(window, app)
        assert window.selected_variant == "guitar" and panel.parts.currentData() == 1
        assert window.base_pitch_semitones == 2 and window.key.value() == 0
        assert "--expected-sha256" in jobs[-1]
        assert_practice_state(window, position, key=0)
        assert_current_measure_visible(panel)
        assert [job[0] for job in jobs] == ["magic-import", "pitch"]
        assert all(path.read_bytes() == content for path, content in plan_bytes.items())
        assert hash_file(archive) == original_archive
    finally:
        dispose(window, app)


def test_checkbox_subset_and_empty_selection_have_one_prepare_action(
    native_window, app, monkeypatch,
):
    window = native_window
    seed_combination(window, "no_guitar_drums")
    jobs, start_job = [], window.start_job

    def record(args):
        jobs.append(args)
        start_job(args)

    monkeypatch.setattr(window, "start_job", record)
    QTest.mouseClick(window.clear_variants, Qt.MouseButton.LeftButton)
    assert not window.prepare_button.isEnabled()
    assert "하나 이상" in window.selection_summary.text()
    window.prepare()
    assert not jobs
    window.models.setCurrentIndex(window.models.findData("auto"))
    for identifier in ("no_guitar", "no_drums", "keys"):
        checkbox = window.variant_checks[identifier]
        window.controller.ensureWidgetVisible(checkbox)
        QTest.mouseClick(checkbox, Qt.MouseButton.LeftButton)
    assert window.checked_variants() == ["no_guitar", "no_drums", "keys"]
    assert "3개" in window.selection_summary.text()
    assert "기타 + 드럼 없는 반주" in window.selection_summary.text()
    assert "키보드" in window.separation_hint.text()
    QTest.mouseClick(window.prepare_button, Qt.MouseButton.LeftButton)
    finish(window, app)
    assert window.selected_variant == "no_guitar_drums"
    start, end = jobs[0].index("--variants") + 1, jobs[0].index("--device")
    assert jobs[0][start:end] == ["no_guitar", "no_drums", "keys", "no_guitar_drums"]
    assert [job[0] for job in jobs] == ["separate", "pitch"]


def test_combined_checkboxes_export_and_reopen_as_one_real_audio_choice(
    native_window, app, tmp_path, monkeypatch,
):
    window = native_window
    identifier = "no_vocals_guitar"
    seed_combination(window, identifier)
    position = practice_state(window, app)
    timeline = window.score_panel.timeline_path.read_bytes()
    source_hash = hash_file(window.source_path)
    window.models.setCurrentIndex(window.models.findData("auto"))
    window.select_preparation_variants(False)
    window.variant_checks["no_vocals"].setChecked(True)
    window.variant_checks["no_guitar"].setChecked(True)
    assert "보컬 + 기타 없는 반주" in window.selection_summary.text()
    QTest.mouseClick(window.prepare_button, Qt.MouseButton.LeftButton)
    finish(window, app)
    assert window.selected_variant == identifier
    assert window.playback_variants.currentText() == "보컬 + 기타 없는 반주"
    assert window.active_variant["targets"] == ["vocals", "guitar"]
    assert_practice_state(window, position)
    QTest.mouseClick(window.cached_audio_button, Qt.MouseButton.LeftButton)
    finish(window, app)
    assert window.selected_variant == identifier
    assert_practice_state(window, position)

    output = tmp_path / "combined.magic"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_: (str(output), "Magic"))
    errors = []

    def choose():
        dialog = app.activeModalWidget()
        try:
            choices = dialog.findChild(QListWidget, "magicAudioVariants")
            first = dialog.findChild(QComboBox, "magicDefaultVariant")
            assert first.currentData() == identifier
            found = False
            for index in range(choices.count()):
                item = choices.item(index)
                item_id = item.data(Qt.ItemDataRole.UserRole)
                if item_id == identifier:
                    found = True
                    assert item.text() == "보컬 + 기타 없는 반주"
                item.setCheckState(
                    Qt.CheckState.Checked if item_id in {"no_vocals", "no_guitar", identifier}
                    else Qt.CheckState.Unchecked
                )
            assert found and first.currentData() == identifier
            QTest.mouseClick(
                dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Save),
                Qt.MouseButton.LeftButton,
            )
        except Exception as error:
            errors.append(str(error))
            dialog.reject()

    QTimer.singleShot(0, choose)
    QTest.mouseClick(window.magic_button, Qt.MouseButton.LeftButton)
    assert not errors
    finish(window, app)
    assert source_hash == hash_file(window.source_path)
    assert timeline == window.score_panel.timeline_path.read_bytes()
    with zipfile.ZipFile(output) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    assert manifest["version"] == 5 and manifest["default_audio_variant"] == identifier
    combined = next(item for item in manifest["audio_variants"] if item["id"] == identifier)
    assert combined["targets"] == ["vocals", "guitar"]
    assert combined["audio"] == manifest["audio"]
    assert combined["audio"]["pitch_semitones"] == 2
    assert combined["audio"]["source_sha256"] == source_hash

    reader = Window(Paths(tmp_path / "reader"))
    reader.resize(1000, 800)
    reader.show()
    reader.audio_output.setVolume(0)
    try:
        reader.import_magic(output)
        finish(reader, app)
        assert reader.selected_variant == identifier
        assert reader.playback_variants.count() == 3
        assert reader.playback_variants.currentText() == "보컬 + 기타 없는 반주"
        assert reader.active_variant["targets"] == ["vocals", "guitar"]
        assert reader.base_pitch_semitones == 2 and reader.key.value() == 0
        assert not reader.player.isPlaying()
        select_option(reader.playback_variants, "no_guitar")
        finish(reader, app)
        select_option(reader.playback_variants, identifier)
        finish(reader, app)
        assert reader.selected_variant == identifier and not reader.player.isPlaying()
    finally:
        dispose(reader, app)


def test_saved_audio_loads_only_actual_cache_and_empty_lookup_keeps_current_song(
    app, tmp_path, tone, monkeypatch,
):
    paths = Paths(tmp_path / "project")
    paths.create()
    source = paths.targets / "cached.wav"
    source.write_bytes(tone.read_bytes())
    result = process(source, Settings("sw6"), paths, stem_runner=SixStemEngine(),
                     variant_id="no_guitar")
    cached_files = {path: hash_file(path) for path in result.instrumental.parent.iterdir()
                    if path.is_file()}
    window = Window(paths)
    window.resize(1000, 800)
    window.show()
    window.audio_output.setVolume(0)
    jobs, start_job = [], window.start_job

    def record(args):
        jobs.append(args)
        start_job(args)

    monkeypatch.setattr(window, "start_job", record)
    try:
        assert window.checked_variants() == ["no_vocals"]
        assert "no_vocals" not in result.variants
        QTest.mouseClick(window.cached_audio_button, Qt.MouseButton.LeftButton)
        finish(window, app)
        assert [job[0] for job in jobs] == ["cached-audio"]
        assert set(window.prepared_variants) == set(result.variants)
        assert window.playback_variants.findData("no_vocals") == -1
        assert "캐시" in window.available_variants.text()
        assert all(hash_file(path) == checksum for path, checksum in cached_files.items())
        select_option(window.playback_variants, "no_guitar")
        finish(window, app)
        window.player.setPosition(1000)
        before = (window.player.source(), window.source_path, window.selected_variant,
                  dict(window.prepared_variants))
        uncached = paths.targets / "uncached.wav"
        uncached.write_bytes(source.read_bytes() + b"different source identity")
        window.refresh_tracks()
        window.tracks.setCurrentRow(1)
        QTest.mouseClick(window.cached_audio_button, Qt.MouseButton.LeftButton)
        finish(window, app)
        assert "저장된 음원이 없습니다" in window.status.text()
        assert (window.player.source(), window.source_path, window.selected_variant,
                window.prepared_variants) == before
        assert window.player.position() == 1000 and not window.player.isPlaying()
        assert [job[0] for job in jobs] == ["cached-audio", "pitch", "cached-audio"]
        assert not (paths.processed / hash_file(uncached)).exists()

        active = dict(window.active_variant)
        current_audio = Path(active["path"])
        current_audio.write_bytes(current_audio.read_bytes() + b"changed cached audio")
        window.tracks.setCurrentRow(0)
        QTest.mouseClick(window.cached_audio_button, Qt.MouseButton.LeftButton)
        finish(window, app)
        assert not window.prepared_variants and window.playback_variants.count() == 0
        assert window.active_variant == active and not window.magic_button.isEnabled()
        window.key.setValue(1)
        QTest.mouseClick(window.key_apply, Qt.MouseButton.LeftButton)
        finish(window, app, error=True)
        assert "변경" in window.job_error
        assert window.pitch_semitones == 0 and window.player.source() == before[0]
    finally:
        dispose(window, app)


def test_collapsed_options_keep_audio_choices_transport_and_score_state(
    native_window, app,
):
    window = native_window
    position = practice_state(window, app, key=0)
    panel, timeline = window.score_panel, window.score_panel.timeline
    window.resize(1000, 800)
    window.main_splitter.setSizes([440, 540])
    QTest.qWait(150)
    expanded_sizes = window.main_splitter.sizes()
    score_width = panel.width()
    QTest.mouseClick(window.options_toggle, Qt.MouseButton.LeftButton)
    QTest.qWait(150)
    assert not window.controller.isVisible() and panel.width() > score_width
    assert window.options_toggle.text() == "옵션 펼치기"
    assert window.play_button.isVisible() and window.clock.isVisible()
    assert window.playback_variants.isVisible() and window.playback_variants.isEnabled()
    select_option(window.playback_variants, "guitar")
    finish(window, app)
    assert panel.timeline is timeline
    assert_practice_state(window, position, key=0)
    assert_current_measure_visible(panel)
    QTest.mouseClick(window.options_toggle, Qt.MouseButton.LeftButton)
    QTest.qWait(150)
    assert window.controller.isVisible()
    assert abs(window.main_splitter.sizes()[0] - expanded_sizes[0]) <= 2
    QTest.mouseClick(window.options_toggle, Qt.MouseButton.LeftButton)
    window.preferences.sync()
    reopened = Window(window.paths)
    reopened.resize(800, 600)
    reopened.show()
    try:
        QTest.qWait(100)
        assert not reopened.controller.isVisible()
        assert reopened.score_panel.isVisible() and reopened.options_toggle.isVisible()
        assert reopened.play_button.isVisible() and reopened.playback_variants.isVisible()
        QTest.mouseClick(reopened.score_visible, Qt.MouseButton.LeftButton)
        assert reopened.controller.isVisible() and not reopened.score_panel.isVisible()
        assert reopened.options_toggle.isChecked()
        QTest.mouseClick(reopened.options_toggle, Qt.MouseButton.LeftButton)
        assert not reopened.controller.isVisible() and reopened.score_panel.isVisible()
        assert reopened.score_visible.isChecked()
        assert reopened.options_toggle.geometry().bottom() < reopened.height()
    finally:
        dispose(reopened, app)


@pytest.mark.parametrize("size,theme", [
    ((800, 560), "dark"), ((800, 600), "light"), ((1366, 768), "dark"),
])
def test_play_seek_and_clock_remain_reachable_with_options_scrolled_or_hidden(
    app, tmp_path, tone, size, theme,
):
    window = Window(Paths(tmp_path / "transport"))
    window.resize(*size)
    window.theme.setCurrentIndex(window.theme.findData(theme))
    window.output_path = tone
    window.player.setSource(QUrl.fromLocalFile(str(tone)))
    window.audio_output.setVolume(0)
    window.set_busy(False)
    window.show()
    try:
        wait_until(app, lambda: window.player.isSeekable() and window.player.duration() > 0)
        available = None
        if app.platformName() not in {"offscreen", "minimal"}:
            # Native CI desktops can be smaller than the requested notebook viewport.
            available = window.screen().availableGeometry()
            frame_size = window.frameGeometry().size() - window.size()
            client_limit = available.size() - frame_size
            size = (min(size[0], client_limit.width()), min(size[1], client_limit.height()))
            assert size[0] >= 800 and size[1] >= 560
            window.resize(*size)
            window.move(available.topLeft())
        window.available_variants.setText(".magic 15개")
        window.playback_variants.addItem("보컬·기타·드럼 없는 반주 — 저장된 연습 음원")
        window.controller.verticalScrollBar().setValue(
            window.controller.verticalScrollBar().maximum()
        )
        for expanded in (True, False, True):
            window.options_toggle.setChecked(expanded)
            QTest.qWait(50)
            assert window.size().toTuple() == size
            rectangles = {}
            for name in ("play_button", "seek", "clock", "playback_variants", "options_toggle"):
                widget = getattr(window, name)
                rectangle = QRect(widget.mapTo(window, QPoint()), widget.size())
                rectangles[name] = rectangle
                assert widget.isVisible() and widget.width() > 0 and widget.height() > 0
                assert window.rect().contains(rectangle), (name, rectangle, window.rect())
                assert widget.visibleRegion().contains(widget.rect()), name
                assert window.childAt(rectangle.center()) is widget, name
                if available is not None:
                    on_screen = QRect(widget.mapToGlobal(QPoint()), widget.size())
                    assert available.contains(on_screen), (name, on_screen, available)
            play, seek = rectangles["play_button"], rectangles["seek"]
            assert play.right() < seek.left()
            assert play.top() <= seek.center().y() <= play.bottom()
            assert play.width() >= 92 and play.height() >= 40
            assert seek.width() >= 120
            assert window.playback_variants.width() >= 130
            assert window.play_button.isEnabled() and window.play_button.text() == "재생"
            window.seek_to(0)
            QTest.mouseClick(window.play_button, Qt.MouseButton.LeftButton)
            wait_until(app, lambda: window.player.isPlaying() and window.player.position() > 50)
            assert window.play_button.text() == "일시정지"
            QTest.mouseClick(window.play_button, Qt.MouseButton.LeftButton)
            wait_until(app, lambda: not window.player.isPlaying())
            assert window.play_button.text() == "재생"
    finally:
        dispose(window, app)


@pytest.mark.parametrize("outcome", ["cancelled", "failed"])
def test_cache_model_switch_commits_catalog_only_after_success(
    app, tmp_path, tone, fake_engine, monkeypatch, outcome,
):
    paths = Paths(tmp_path / "project")
    paths.create()
    source = paths.targets / "study.wav"
    source.write_bytes(tone.read_bytes())
    process(source, Settings("hyperace"), paths, runner=fake_engine)

    def quieter_model(audio, output, *_args, **_kwargs):
        samples, rate = sf.read(audio, dtype="float32")
        sf.write(output, samples * .5, rate, subtype="FLOAT")
        return "test"

    preferred = process(source, Settings("leap"), paths, runner=quieter_model)
    window = Window(paths)
    window.resize(1000, 800)
    window.show()
    window.audio_output.setVolume(0)
    window.models.setCurrentIndex(window.models.findData("hyperace"))
    try:
        QTest.mouseClick(window.prepare_button, Qt.MouseButton.LeftButton)
        finish(window, app)
        window.player.setPosition(1000)
        before = (window.output_path, window.base_instrumental, window.player.source(),
                  dict(window.prepared_variants), dict(window.active_variant))
        assert window.active_variant["model_key"] == "hyperace"
        assert window.active_variant["sha256"] != preferred.variants["no_vocals"].sha256
        start_job, interrupted = window.start_job, []

        def interrupt_switch(args):
            if args[0] == "pitch" and not interrupted:
                interrupted.append(args)
                if outcome == "failed":
                    target = Path(args[args.index("--input") + 1])
                    target.write_bytes(target.read_bytes() + b"changed after cache lookup")
                start_job(args)
                assert window.status.isVisible() and window.progress.isVisible()
                assert window.cancel_button.isVisible() and window.cancel_button.isEnabled()
                if outcome == "cancelled":
                    QTimer.singleShot(0, window.cancel_button.click)
            else:
                start_job(args)

        monkeypatch.setattr(window, "start_job", interrupt_switch)
        QTest.mouseClick(window.cached_audio_button, Qt.MouseButton.LeftButton)
        QTest.mouseClick(window.options_toggle, Qt.MouseButton.LeftButton)
        finish(window, app, error=outcome == "failed")
        assert len(interrupted) == 1
        assert (window.output_path, window.base_instrumental, window.player.source(),
                window.prepared_variants, window.active_variant) == before
        assert window._pending_catalog is None
        assert window.player.position() == 1000 and not window.player.isPlaying()
        assert window.status.isVisible() and not window.controller.isVisible()
        if outcome == "cancelled":
            assert "취소" in window.status.text()
        else:
            assert window.status.text() == window.job_error

        monkeypatch.setattr(window, "start_job", start_job)
        QTest.mouseClick(window.options_toggle, Qt.MouseButton.LeftButton)
        window.key.setValue(1)
        QTest.mouseClick(window.key_apply, Qt.MouseButton.LeftButton)
        finish(window, app)
        assert window.pitch_semitones == 1 and window.base_instrumental == before[1]
        assert window.active_variant == before[4]
    finally:
        dispose(window, app)
