# SPDX-License-Identifier: GPL-3.0-only
"""Exercise cached listening choices and imported score pages with real Qt workers."""

import json
import zipfile
from pathlib import Path

import pytest
import soundfile as sf
from PySide6.QtCore import QCoreApplication, QEvent, Qt, QTimer
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
from practice_player.config import Paths, Settings
from practice_player.gui import Window
from practice_player.magic_bundle import export_magic
from practice_player.magic_import import import_magic, load_imported
from practice_player.page_images import PageImages
from practice_player.pipeline import process
from practice_player.score import ScoreTimeline
from practice_player.variants import SUPPORTED_VARIANTS
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
    window.separation_variant.setCurrentIndex(window.separation_variant.findData(identifier))
    QTest.mouseClick(window.prepare_button, Qt.MouseButton.LeftButton)
    finish(window, app)
    assert window.selected_variant == identifier


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
    if all_variants:
        window.separation_variant.setCurrentIndex(window.separation_variant.findData("no_guitar"))
        QTest.mouseClick(window.prepare_all_button, Qt.MouseButton.LeftButton)
        finish(window, app)
        assert set(window.prepared_variants) == set(SUPPORTED_VARIANTS)
        assert "--all-variants" in jobs[0] and jobs[0][jobs[0].index("--model") + 1] == "auto"
    else:
        prepare_variant(window, app, "no_guitar")
    assert window.selected_variant == "no_guitar"
    assert [job[0] for job in jobs] == ["separate", "pitch"]
    assert "--expected-sha256" in jobs[1] and "--expected-frames" in jobs[1]
    assert window.job_result["type"] == "pitch_result"
    assert window.base_instrumental == Path(window.prepared_variants["no_guitar"]["path"])
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
        with other.open("ab") as stream:
            stream.write(b"changed after preparation")
        select_option(window.playback_variants, "guitar")
    else:
        window.models.setCurrentIndex(window.models.findData("hyperace"))
        window.separation_variant.setCurrentIndex(window.separation_variant.findData("guitar"))
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
