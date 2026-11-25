# SPDX-License-Identifier: GPL-3.0-only
"""Cache destinations survive restarts and are honored by the real workers."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from practice_player.cache import discover_cached_audio, hash_file
from practice_player.cache_locations import SETTINGS_NAME, save_cache_locations
from practice_player.cli import EVENT_PREFIX
from practice_player.config import ROOT, Paths


def configured(tmp_path):
    paths = Paths(tmp_path / "project")
    return save_cache_locations(paths, tmp_path / "음원 저장", tmp_path / "악보 저장")


def worker(command, *arguments):
    result = subprocess.run(
        [sys.executable, str(ROOT / "main.py"), command, *map(str, arguments), "--events"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return [json.loads(line[len(EVENT_PREFIX):]) for line in result.stdout.splitlines()
            if line.startswith(EVENT_PREFIX)]


def test_default_paths_are_legacy_and_reading_does_not_create_folders(tmp_path):
    paths = Paths(tmp_path / "not-created")
    assert paths.audio_cache == paths.score_cache == paths.processed
    assert paths.processed == paths.root / "processed"
    assert not paths.cache_location_error
    paths.require_cache_locations()
    assert not paths.root.exists()


def test_save_restart_and_reset_preserve_existing_files_and_preferences(tmp_path):
    paths = Paths(tmp_path / "project")
    paths.create()
    original = paths.processed / "existing-cache.wav"
    original.write_bytes(b"preserve existing cache")
    preferences = paths.processed / "ui.ini"
    preferences.write_text("theme=dark\n", encoding="utf-8")
    selected = save_cache_locations(paths, tmp_path / "audio", tmp_path / "score")
    restarted = Paths(paths.root)
    assert restarted == selected
    assert restarted.audio_cache == tmp_path / "audio"
    assert restarted.score_cache == tmp_path / "score"
    assert restarted.processed == paths.processed
    assert original.read_bytes() == b"preserve existing cache"
    assert preferences.read_text(encoding="utf-8") == "theme=dark\n"
    assert not list(selected.audio_cache.iterdir()) and not list(selected.score_cache.iterdir())
    reset = save_cache_locations(restarted, paths.processed, paths.processed)
    assert reset.audio_cache == reset.score_cache == paths.processed
    saved = json.loads((paths.processed / SETTINGS_NAME).read_text(encoding="utf-8"))
    assert saved == {"version": 1, "audio": None, "score": None}
    assert original.read_bytes() == b"preserve existing cache"


@pytest.mark.parametrize("invalid", ["empty", "relative", "file", "link"])
def test_invalid_second_folder_does_not_commit_first_folder(tmp_path, invalid):
    paths = configured(tmp_path)
    settings = paths.processed / SETTINGS_NAME
    original = settings.read_bytes()
    value = {"empty": "", "relative": "relative-folder"}.get(invalid)
    if invalid == "file":
        value = tmp_path / "ordinary-file"
        value.write_bytes(b"do not overwrite")
    if invalid == "link":
        value = tmp_path / "folder-link"
        value.symlink_to(paths.score_cache, target_is_directory=True)
    with pytest.raises(ValueError):
        save_cache_locations(paths, tmp_path / "new audio", value)
    assert settings.read_bytes() == original
    assert Paths(paths.root) == paths
    assert not list(tmp_path.rglob(".magic-write-check-*"))
    assert not list(paths.processed.glob(".cache-locations-*.json"))


def test_unwritable_folder_keeps_the_entire_previous_configuration(tmp_path, monkeypatch):
    import practice_player.cache_locations as module

    paths = configured(tmp_path)
    previous = (paths.processed / SETTINGS_NAME).read_bytes()
    probe = module.tempfile.TemporaryFile

    def denied(*args, **kwargs):
        if Path(kwargs["dir"]) == tmp_path / "denied":
            raise PermissionError("test folder denies writes")
        return probe(*args, **kwargs)

    monkeypatch.setattr(module.tempfile, "TemporaryFile", denied)
    with pytest.raises(ValueError, match="악보 분석 캐시 폴더에 저장할 수 없습니다"):
        save_cache_locations(paths, tmp_path / "new audio", tmp_path / "denied")
    assert (paths.processed / SETTINGS_NAME).read_bytes() == previous
    assert Paths(paths.root) == paths
    assert not list(tmp_path.rglob(".magic-write-check-*"))


def test_configuration_publication_failure_keeps_old_roots(tmp_path, monkeypatch):
    import practice_player.cache_locations as module

    paths = configured(tmp_path)
    settings = paths.processed / SETTINGS_NAME
    previous = settings.read_bytes()

    def failed_replace(*_):
        raise PermissionError("test publication denied")

    monkeypatch.setattr(module.os, "replace", failed_replace)
    with pytest.raises(ValueError, match="기존 위치를 유지"):
        save_cache_locations(paths, tmp_path / "new audio", tmp_path / "new score")
    assert settings.read_bytes() == previous
    assert Paths(paths.root) == paths
    assert not list(paths.processed.glob(".cache-locations-*.json"))


@pytest.mark.parametrize("filename", [SETTINGS_NAME, ".cache-locations.lock"])
@pytest.mark.parametrize("nested", [False, True])
def test_reserved_setting_paths_are_refused_before_directory_creation(tmp_path, filename, nested):
    paths = Paths(tmp_path / "project")
    reserved = paths.processed / filename
    destination = reserved / "cache" if nested else reserved
    with pytest.raises(ValueError, match="프로그램 설정 파일"):
        save_cache_locations(paths, tmp_path / "audio", destination)
    assert not reserved.exists()
    assert not paths.processed.exists()
    recovered = save_cache_locations(paths, tmp_path / "audio", tmp_path / "score")
    assert not recovered.cache_location_error


@pytest.mark.parametrize("content", [
    "{broken",
    '{"version":2,"audio":null,"score":null}',
    '{"version":1,"audio":"relative","score":null}',
    " " * (16 * 1024 + 1),
])
def test_invalid_saved_settings_are_visible_block_cache_work_and_can_be_repaired(
    tmp_path, tone, content,
):
    root = tmp_path / "project"
    (root / "processed").mkdir(parents=True)
    (root / "processed" / SETTINGS_NAME).write_text(content, encoding="utf-8")
    paths = Paths(root)
    assert "상세 옵션" in paths.cache_location_error
    with pytest.raises(ValueError, match="상세 옵션"):
        discover_cached_audio(tone, paths)
    assert not (paths.processed / hash_file(tone)).exists()
    repaired = save_cache_locations(paths, paths.processed, paths.processed)
    assert not repaired.cache_location_error
    assert discover_cached_audio(tone, repaired)["variants"] == {}


def test_other_window_changes_are_not_silently_overwritten_or_used(tmp_path, tone):
    paths = configured(tmp_path)
    other = Paths(paths.root)
    updated = save_cache_locations(other, tmp_path / "other audio", other.score_cache)
    settings = (paths.processed / SETTINGS_NAME).read_bytes()
    with pytest.raises(ValueError, match="다른 창"):
        discover_cached_audio(tone, paths)
    with pytest.raises(ValueError, match="다른 창"):
        save_cache_locations(paths, paths.audio_cache, paths.score_cache)
    assert (paths.processed / SETTINGS_NAME).read_bytes() == settings
    assert Paths(paths.root) == updated


def test_real_audio_workers_follow_saved_folder_and_reuse_after_reopening(tmp_path, tone):
    paths = configured(tmp_path)
    events = worker(
        "separate", "--input", tone, "--root", paths.root, "--variant", "original",
    )
    prepared = next(event for event in events if event["type"] == "result")
    audio = Path(prepared["instrumental"])
    assert paths.audio_cache in audio.parents
    events = worker("cached-audio", "--input", tone, "--root", paths.root)
    cached = next(event for event in events if event["type"] == "cached_audio")
    assert set(cached["variants"]) == {"original"}
    assert Path(cached["variants"]["original"]["path"]) == audio
    events = worker("pitch", "--input", audio, "--root", paths.root, "--semitones", "2")
    pitched = Path(next(event for event in events if event["type"] == "pitch_result")["path"])
    assert paths.audio_cache / "pitch" in pitched.parents
    assert audio.is_file() and pitched.is_file()
    assert not (paths.processed / hash_file(tone)).exists()
    assert not (paths.processed / "pitch").exists()
    moved = save_cache_locations(paths, tmp_path / "empty audio", paths.score_cache)
    assert discover_cached_audio(tone, moved)["variants"] == {}
    assert audio.is_file() and pitched.is_file()
    restored = save_cache_locations(moved, paths.audio_cache, paths.score_cache)
    assert Path(discover_cached_audio(tone, restored)["variants"]["original"]["path"]) == audio


def test_real_score_worker_and_view_cache_use_independent_score_folder(tmp_path, tone):
    from practice_player.score import ScoreTimeline, load_last_timeline
    from practice_player.score_layout import analyze_scroll
    from practice_player.score_view import view_profile
    from tests.test_pdf_only_analysis import score_pdf

    paths = configured(tmp_path)
    pdf = score_pdf(tmp_path / "original-score.pdf")
    original = pdf.read_bytes()
    events = worker(
        "score-analyze", "--audio", tone, "--pdf", pdf, "--root", paths.root,
        "--mode", "pdf", "--bpm", "120",
    )
    timeline = Path(next(event for event in events if event["type"] == "score_result")["path"])
    assert paths.score_cache in timeline.parents
    plan = ScoreTimeline.load(timeline)
    plan.data.update(reviewed=True, offset=0.125)
    plan.save(timeline)
    reviewed = timeline.read_bytes()
    reopened = load_last_timeline(pdf, Paths(paths.root), hash_file(tone))
    assert reopened is not None and reopened[0] == timeline
    viewed = analyze_scroll(tone, pdf, paths, timeline, view_profile(2, 600, 450))
    assert paths.score_cache in viewed.parents
    assert timeline.read_bytes() == reviewed
    assert ScoreTimeline.load(viewed).data["offset"] == 0.125
    assert pdf.read_bytes() == original
    assert not list(paths.audio_cache.iterdir())


def test_imported_bundle_stays_atomic_in_selected_audio_folder(tmp_path):
    from practice_player.magic_import import import_magic, load_imported
    from tests.test_magic_import import make_package

    paths = configured(tmp_path)
    archive = make_package(tmp_path / "portable", gp=True)
    imported = import_magic(archive, paths)
    bundle_path = Path(imported["bundle_path"])
    assert paths.audio_cache / "imported-magic" in bundle_path.parents
    bundle = load_imported(bundle_path)
    assert all(paths.audio_cache in Path(item["path"]).parents
               for item in bundle["audio_variants"])
    assert all(paths.audio_cache in Path(part["timeline"]).parents for part in bundle["parts"])
    assert import_magic(archive, Paths(paths.root))["cached"]
    assert not list(paths.score_cache.iterdir())


def test_gp_render_and_collection_use_score_folder(tmp_path, tone):
    from practice_player.gp_collection import prepare_collection
    from practice_player.gp_render import prepare_gp
    from practice_player.score_analysis import analyze_score
    from tests.test_guitar_pro import gp_song

    paths = configured(tmp_path)
    gp = gp_song(tmp_path / "original.gp5")
    prepared = prepare_gp(gp, paths)
    assert paths.score_cache / "gp-scores" in Path(prepared["pdf"]).parents
    display = {key: prepared[key] for key in ("pdf", "layout", "source", "signature")}
    timeline = analyze_score(
        tone, Path(prepared["pdf"]), paths, musicxml=gp, gp_display=display, mode="tempo",
    )
    collection = prepare_collection(timeline, paths)
    assert all(paths.score_cache / "gp-collections" in Path(item["timing"]).parents
               for item in collection)
    assert prepare_gp(gp, Paths(paths.root))["cached"]
    assert not list(paths.audio_cache.iterdir())


def test_combination_cache_and_read_only_discovery_use_audio_folder(tmp_path, tone, monkeypatch):
    from practice_player.combinations import process_combination
    from tests.test_audio_combinations import SequentialTestEngine

    paths = configured(tmp_path)
    engine = SequentialTestEngine()
    monkeypatch.setattr("practice_player.engine.separate_stems", engine)
    result = process_combination(tone, "no_vocals_guitar", "auto", "cpu", False, paths)
    assert paths.audio_cache in result.instrumental.parents
    before = result.instrumental.read_bytes()
    discovered = discover_cached_audio(tone, Paths(paths.root))
    assert "no_vocals_guitar" in discovered["variants"]
    assert result.instrumental.read_bytes() == before
    assert len(engine.calls) == 2
    assert not list(paths.score_cache.iterdir())


@pytest.mark.parametrize("change_during_export", [False, True])
def test_export_uses_one_cache_snapshot_for_all_audio_and_gp_parts(
    tmp_path, monkeypatch, change_during_export,
):
    import practice_player.transpose as transpose
    from practice_player.magic_bundle import export_magic
    from tests.test_magic_variants import variant_study

    request, _, initial = variant_study(tmp_path / "project", kind="gp", pitch=2)
    paths = save_cache_locations(initial, tmp_path / "audio", tmp_path / "score")
    output = Path(request["output"])
    output.write_bytes(b"existing exported practice")
    request["overwrite"] = True
    prepare = transpose.prepare_pitch
    destinations = []

    def prepare_then_change(source, worker_paths, semitones, report):
        destinations.append(worker_paths.audio_cache)
        prepared = prepare(source, worker_paths, semitones, report)
        if change_during_export and len(destinations) == 1:
            save_cache_locations(Paths(paths.root), tmp_path / "other audio", paths.score_cache)
        return prepared

    monkeypatch.setattr(transpose, "prepare_pitch", prepare_then_change)
    if change_during_export:
        with pytest.raises(ValueError, match="다른 창"):
            export_magic(request)
        assert output.read_bytes() == b"existing exported practice"
        assert not list((tmp_path / "other audio").iterdir())
    else:
        assert export_magic(request) == output
        assert output.read_bytes()[:2] == b"PK"
    assert destinations == [paths.audio_cache, paths.audio_cache]
    assert list((paths.score_cache / "gp-collections").rglob("collection.json"))
