# SPDX-License-Identifier: GPL-3.0-only
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject

from practice_player.cache import hash_file
from practice_player.config import Paths
from practice_player.score import ScoreTimeline
from practice_player.score_analysis import analyze_score
from practice_player.score_layout import analyze_scroll
from practice_player.score_view import view_profile
from tests.test_score import xml_file
from tests.test_score_geometry import vector_pdf


def score_pdf(path, pattern=(5,)):
    rows = [(1400, [50, 230, 510, 850], []), (650, [50, 230, 510, 850], [])]
    vector_pdf(path, [((900, 1600), rows, pattern)])
    if len(pattern) > 1:
        reader = PdfReader(path)
        writer = PdfWriter()
        page = writer.add_page(reader.pages[0])
        commands = page.get_contents().get_data()
        for y, _, _ in rows:
            low, high = y - (len(pattern) - 1) * 85, y + (pattern[0] - 1) * 7
            commands += f"\n50 {low} m 50 {high} l S\n".encode()
        stream = DecodedStreamObject()
        stream.set_data(commands)
        page[NameObject("/Contents")] = writer._add_object(stream)
        with path.open("wb") as output:
            writer.write(output)
    return path


@pytest.fixture
def pdf_study(tmp_path):
    paths = Paths(tmp_path / "project")
    paths.create()
    return paths, score_pdf(paths.score_targets / "study.pdf")


def forbid_notation(*_args, **_kwargs):
    pytest.fail("PDF-only analysis must not use notation or audio alignment")


@pytest.mark.parametrize("pattern", [(5,), (6,), (5, 5), (5, 6), (5,) * 6])
def test_pdf_without_musicxml_recognizes_bars_and_uses_entered_meter(
    tmp_path, tone, monkeypatch, pattern,
):
    paths = Paths(tmp_path / "project")
    paths.create()
    pdf = score_pdf(paths.score_targets / "score.pdf", pattern)
    original = pdf.read_bytes()
    for name in ("run_omr", "parse_notation", "align_audio", "matching_musicxml"):
        monkeypatch.setattr(f"practice_player.score_analysis.{name}", forbid_notation)
    result = analyze_score(
        tone, pdf, paths, mode="pdf", bpm=90, meter_beats=6, meter_unit=8, lead_in=0.5,
    )
    plan = ScoreTimeline.load(result)
    assert plan.data["notation_source"]["method"] == "pdf-geometry"
    assert len(plan.cues) == len(plan.data["originals"]) == 6
    assert [cue["start"] for cue in plan.cues] == [0.5, 2.5, 4.5, 6.5, 8.5, 10.5]
    assert plan.end == 12.5
    assert all(cue["beats"] == 3 for cue in plan.cues)
    assert not plan.data["reviewed"]
    assert "musicxml_path" not in plan.data and "xml_sha256" not in plan.data
    assert plan.data["pdf_layout"]["method"] == "pdf-only-staves-barlines"
    assert any("반복" in warning for warning in plan.data["warnings"])
    assert not list(paths.processed.rglob("recognition"))
    assert pdf.read_bytes() == original


def test_pdf_route_cache_meter_and_force_preserve_reviewed_timing(pdf_study, tone):
    paths, pdf = pdf_study
    settings = dict(mode="pdf", bpm=120, route_override="1-3,1-2,4-6")
    result = analyze_score(tone, pdf, paths, **settings)
    plan = ScoreTimeline.load(result)
    assert [cue["source_index"] for cue in plan.cues] == [0, 1, 2, 0, 1, 3, 4, 5]
    assert [cue["visit"] for cue in plan.cues] == [1, 1, 1, 2, 2, 1, 1, 1]
    plan.data.update(reviewed=True, offset=0.125)
    plan.save(result)
    reviewed_bytes = result.read_bytes()
    assert analyze_score(tone, pdf, paths, **settings) == result
    changed_meter = analyze_score(tone, pdf, paths, **settings, meter_beats=3)
    forced = analyze_score(tone, pdf, paths, **settings, force=True)
    assert len({result, changed_meter, forced}) == 3
    assert result.read_bytes() == reviewed_bytes
    assert ScoreTimeline.load(changed_meter).cues[1]["start"] == 1.5


def test_pdf_scroll_reuses_timing_without_xml_and_checks_source_changes(
    pdf_study, tone, monkeypatch,
):
    paths, pdf = pdf_study
    base = analyze_score(tone, pdf, paths, mode="pdf", route_override="1-6,1-3")
    plan = ScoreTimeline.load(base)
    plan.data.update(reviewed=True, offset=0.25)
    plan.save(base)
    monkeypatch.setattr("practice_player.score_layout.matching_musicxml", forbid_notation)
    profile = view_profile(2, 420, 360)
    result = analyze_scroll(tone, pdf, paths, base, profile)
    view = ScoreTimeline.load(result)
    assert view.cues == plan.cues and view.data["offset"] == 0.25
    assert view.data["reviewed"]
    frames = view.data["scroll"]["frames"]
    assert all(frame["y0"] == frame["y1"] for frame in frames)
    assert frames[0]["y0"] == frames[1]["y0"] < frames[3]["y0"]
    assert frames[6]["y0"] == frames[0]["y0"]
    assert analyze_scroll(tone, pdf, paths, result, profile) == result
    pdf.write_bytes(pdf.read_bytes() + b"\n% source changed\n")
    with pytest.raises(ValueError, match="다릅니다"):
        analyze_scroll(tone, pdf, paths, base, profile)


@pytest.mark.parametrize("settings", [
    {"meter_beats": 0}, {"meter_beats": True}, {"meter_unit": 3},
    {"bpm": float("nan")}, {"route_override": "1-99"},
])
def test_invalid_pdf_settings_do_not_publish_timing(pdf_study, tone, settings):
    paths, pdf = pdf_study
    with pytest.raises(ValueError):
        analyze_score(tone, pdf, paths, mode="pdf", **settings)
    assert not list(paths.processed.rglob("timeline.json"))


def test_pdf_change_during_detection_does_not_publish_timing(pdf_study, tone, monkeypatch):
    from practice_player import score_geometry

    paths, pdf = pdf_study
    detect = score_geometry.detect_pdf_layout

    def changed(*args, **kwargs):
        result = detect(*args, **kwargs)
        pdf.write_bytes(pdf.read_bytes() + b"\n% changed during detection\n")
        return result

    monkeypatch.setattr(score_geometry, "detect_pdf_layout", changed)
    with pytest.raises(ValueError, match="변경"):
        analyze_score(tone, pdf, paths, mode="pdf")
    assert not list(paths.processed.rglob("timeline.json"))


def test_pdf_only_cli_completes_without_musicxml(pdf_study, tone):
    paths, pdf = pdf_study
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / "main.py"), "score-analyze", "--root", str(paths.root),
         "--audio", str(tone), "--pdf", str(pdf), "--mode", "pdf", "--bpm", "90",
         "--meter-beats", "6", "--meter-unit", "8", "--zoom", "page", "--events"],
        cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    events = [json.loads(line[len("@@PRACTICE@@"):]) for line in result.stdout.splitlines()
              if line.startswith("@@PRACTICE@@")]
    completed = next(event for event in events if event["type"] == "score_result")
    plan = ScoreTimeline.load(Path(completed["path"]))
    assert plan.data["alignment"]["meter_unit"] == 8
    assert plan.cues[1]["start"] == 2
    assert len(plan.data["scroll"]["frames"]) == 6


@pytest.mark.parametrize("saved_mode", [None, "audio", "tempo", "pdf"])
def test_pdf_gui_defaults_and_worker_arguments(pdf_study, tone, monkeypatch, saved_mode):
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication, QCheckBox, QComboBox, QDialog, QPushButton, QSpinBox

    from practice_player.cli import parser
    from practice_player.gui import Window
    from practice_player.score_gui import ScorePanel

    app = QApplication.instance() or QApplication([])
    paths, pdf = pdf_study
    panel = ScorePanel(paths)
    panel.audio, panel.source_hash = tone, hash_file(tone)
    panel.resize(700, 700)
    panel.open_pdf(pdf)
    if saved_mode is not None:
        panel.timeline = ScoreTimeline.load(analyze_score(tone, pdf, paths, mode="pdf"))
        panel.timeline.data["signature"]["mode"] = saved_mode
    captured = []
    panel.analysis_requested.connect(captured.append)

    def accept(dialog):
        mode = dialog.findChild(QComboBox, "scoreAlignmentMode")
        expected_mode = saved_mode if saved_mode in {"audio", "tempo"} else "auto"
        assert mode.currentData() == expected_mode
        assert any("MusicXML" in button.text() for button in dialog.findChildren(QPushButton))
        for checkbox in dialog.findChildren(QCheckBox):
            if checkbox.text().startswith("기존 마디 시간표"):
                checkbox.setChecked(False)
        beats = dialog.findChild(QSpinBox, "scoreMeterBeats")
        unit = dialog.findChild(QComboBox, "scoreMeterUnit")
        assert beats.isEnabled() and unit.isEnabled()
        beats.setValue(6)
        unit.setCurrentIndex(unit.findData(8))
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(QDialog, "exec", accept)
    try:
        panel.analysis_options()
        options = captured[0]
        jobs = []
        owner = SimpleNamespace(worker=None, paths=paths,
                                player=SimpleNamespace(pause=lambda: None), start_job=jobs.append)
        owner.cache_work_ready = lambda: Window.cache_work_ready(owner)
        Window.analyze_score(owner, options)
        args = parser().parse_args(jobs[0])
        expected_mode = saved_mode if saved_mode in {"audio", "tempo"} else "auto"
        assert args.mode == expected_mode and args.meter_beats == 6 and args.meter_unit == 8
        assert args.musicxml is None
    finally:
        panel.view.clear()
        panel.close()
        panel.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


def same_score_xml(paths, pdf, count=6):
    xml = xml_file(paths.score_targets, [""] * count)
    return xml.rename(pdf.with_suffix(".musicxml"))


def cached_omr(paths, pdf, audio, count=6):
    folder = paths.processed / hash_file(audio) / "scores" / hash_file(pdf) / "recognition/v2"
    folder.mkdir(parents=True)
    xml = same_score_xml(paths, pdf, count).rename(folder / "score.mxl")
    xml.with_suffix(".json").write_text('{"method":"pdf-original"}', encoding="utf-8")
    return xml


def estimated_alignment(_audio, notation, _report):
    return ([float(i) for i in range(len(notation["cues"]))],
            float(len(notation["cues"])), {"method": "test-note-alignment"})


def omr_failure(*_args, **_kwargs):
    raise RuntimeError("음표 인식 결과 없음")


def test_auto_uses_matching_notation_and_preserves_its_cache(pdf_study, tone, monkeypatch):
    paths, pdf = pdf_study
    xml = same_score_xml(paths, pdf)
    monkeypatch.setattr("practice_player.score_analysis.run_omr", forbid_notation)
    monkeypatch.setattr("practice_player.score_analysis.align_audio", estimated_alignment)
    result = analyze_score(tone, pdf, paths)
    plan = ScoreTimeline.load(result)
    assert plan.data["signature"]["mode"] == "auto"
    assert plan.data["xml_sha256"] == hash_file(xml)
    assert "analysis_fallback" not in plan.data
    assert plan.data["alignment"]["method"] == "test-note-alignment"
    plan.data.update(reviewed=True, offset=0.2)
    plan.save(result)
    monkeypatch.setattr("practice_player.score_analysis.align_audio", forbid_notation)
    assert analyze_score(tone, pdf, paths) == result
    assert ScoreTimeline.load(result).data["offset"] == 0.2


def test_auto_uses_successful_omr_before_geometry_fallback(pdf_study, tone, monkeypatch):
    paths, pdf = pdf_study

    def recognized(_pdf, work, _report):
        work.mkdir(parents=True)
        (work / "input.json").write_text('{"method":"pdf-original"}', encoding="utf-8")
        return xml_file(work, [""] * 6)

    monkeypatch.setattr("practice_player.score_analysis.run_omr", recognized)
    monkeypatch.setattr("practice_player.score_analysis.align_audio", estimated_alignment)
    plan = ScoreTimeline.load(analyze_score(tone, pdf, paths))
    assert plan.data["alignment"]["method"] == "test-note-alignment"
    assert "analysis_fallback" not in plan.data


def test_auto_generated_notation_fallback_immediately_reuses_reviewed_cache(
    pdf_study, tone, monkeypatch,
):
    paths, pdf = pdf_study

    def recognized(_pdf, work, _report):
        work.mkdir(parents=True)
        (work / "input.json").write_text('{"method":"pdf-original"}', encoding="utf-8")
        return xml_file(work, [""] * 2)

    monkeypatch.setattr("practice_player.score_analysis.run_omr", recognized)
    result = analyze_score(tone, pdf, paths)
    plan = ScoreTimeline.load(result)
    generated = next(paths.processed.rglob("score.mxl"))
    assert plan.data["analysis_fallback"]["stage"] == "layout"
    assert plan.data["signature"]["automatic"]["notation_sha256"] == hash_file(generated)
    plan.data.update(reviewed=True, offset=0.4)
    plan.save(result)
    saved = result.read_bytes()
    monkeypatch.setattr("practice_player.score_analysis.run_omr", forbid_notation)
    monkeypatch.setattr("practice_player.score_layout.analyze_layout", forbid_notation)
    monkeypatch.setattr("practice_player.score_geometry.detect_pdf_layout", forbid_notation)
    assert analyze_score(tone, pdf, paths) == result
    assert result.read_bytes() == saved


@pytest.mark.parametrize("stage", ["copy", "recognition", "fallback"])
def test_auto_generated_notation_changes_are_not_published(
    pdf_study, tone, monkeypatch, stage,
):
    from practice_player import score_analysis, score_geometry

    paths, pdf = pdf_study

    def recognized(_pdf, work, _report):
        work.mkdir(parents=True)
        (work / "input.json").write_text('{"method":"pdf-original"}', encoding="utf-8")
        return xml_file(work, [""] * 2)

    monkeypatch.setattr(score_analysis, "run_omr", recognized)
    if stage == "copy":
        copyfile = score_analysis.shutil.copyfile

        def changed_copy(source, destination):
            source.write_bytes(source.read_bytes() + b"\n")
            return copyfile(source, destination)

        monkeypatch.setattr(score_analysis.shutil, "copyfile", changed_copy)
    elif stage == "recognition":
        def changed_recognition(_pdf, xml, _report):
            xml.write_bytes(xml.read_bytes() + b"\n")
            raise ValueError("인식 결과와 마디 배치 불일치")

        monkeypatch.setattr("practice_player.score_layout.analyze_layout", changed_recognition)
    else:
        detect = score_geometry.detect_pdf_layout

        def changed_fallback(*args, **kwargs):
            result = detect(*args, **kwargs)
            generated = next(paths.processed.rglob("score.mxl"))
            generated.write_bytes(generated.read_bytes() + b"\n")
            return result

        monkeypatch.setattr(score_geometry, "detect_pdf_layout", changed_fallback)
    with pytest.raises(ValueError, match="변경"):
        analyze_score(tone, pdf, paths)
    assert not list(paths.processed.rglob("timeline.json"))


def test_auto_omr_failure_reuses_fallback_until_request_changes(pdf_study, tone, monkeypatch):
    paths, pdf = pdf_study
    attempts = []

    def failed(*args):
        attempts.append(args)
        omr_failure()

    monkeypatch.setattr("practice_player.score_analysis.run_omr", failed)
    result = analyze_score(tone, pdf, paths, bpm=90, meter_beats=6, meter_unit=8)
    plan = ScoreTimeline.load(result)
    assert plan.data["analysis_fallback"]["stage"] == "omr"
    assert plan.data["signature"]["mode"] == "auto"
    assert plan.cues[1]["start"] == 2
    assert any("인식을 완료하지 못해" in warning for warning in plan.data["warnings"])
    plan.data.update(reviewed=True, offset=0.4)
    plan.save(result)
    original = result.read_bytes()
    settings = dict(bpm=90, meter_beats=6, meter_unit=8)
    assert analyze_score(tone, pdf, paths, **settings) == result
    assert len(attempts) == 1
    changed = analyze_score(tone, pdf, paths, bpm=91, meter_beats=6, meter_unit=8)
    forced = analyze_score(tone, pdf, paths, **settings, force=True)
    assert len(attempts) == 3 and len({result, changed, forced}) == 3
    assert result.read_bytes() == original
    same_score_xml(paths, pdf)
    monkeypatch.setattr("practice_player.score_analysis.align_audio", estimated_alignment)
    recovered = ScoreTimeline.load(analyze_score(tone, pdf, paths, **settings))
    assert "analysis_fallback" not in recovered.data
    assert len(attempts) == 3


def test_auto_layout_mismatch_falls_back_to_printed_bars(pdf_study, tone, monkeypatch):
    paths, pdf = pdf_study
    cached_omr(paths, pdf, tone, count=2)
    monkeypatch.setattr("practice_player.score_analysis.align_audio", forbid_notation)
    plan = ScoreTimeline.load(analyze_score(tone, pdf, paths))
    assert len(plan.cues) == 6
    assert plan.data["analysis_fallback"]["stage"] == "layout"


def test_auto_malformed_notation_can_still_fall_back_for_plain_score(pdf_study, tone):
    paths, pdf = pdf_study
    xml = cached_omr(paths, pdf, tone)
    xml.write_text("<score-partwise><broken>", encoding="utf-8")
    plan = ScoreTimeline.load(analyze_score(tone, pdf, paths))
    assert len(plan.cues) == 6
    assert plan.data["analysis_fallback"]["stage"] == "layout"
    assert xml.read_text(encoding="utf-8") == "<score-partwise><broken>"


@pytest.mark.parametrize("stage", ["notation", "navigation"])
def test_auto_only_note_failure_may_fall_back(pdf_study, tone, monkeypatch, stage):
    paths, pdf = pdf_study
    cached_omr(paths, pdf, tone)

    def failed(*_args, **_kwargs):
        raise ValueError("반복 구조 인식 실패")

    target = ("practice_player.score_analysis.parse_notation" if stage == "notation" else
              "practice_player.score_symbols.supplement_xml")
    monkeypatch.setattr(target, failed)
    if stage == "navigation":
        with pytest.raises(ValueError, match="반복 구조"):
            analyze_score(tone, pdf, paths)
        assert not list(paths.processed.rglob("timeline.json"))
        return
    plan = ScoreTimeline.load(analyze_score(tone, pdf, paths))
    assert plan.data["analysis_fallback"]["stage"] == stage


def test_auto_reports_failure_when_both_recognition_paths_fail(pdf_study, tone, monkeypatch):
    paths, pdf = pdf_study
    monkeypatch.setattr("practice_player.score_analysis.run_omr", omr_failure)

    def no_geometry(*_args, **_kwargs):
        raise ValueError("마디 경계 불명확")

    monkeypatch.setattr("practice_player.score_geometry.detect_pdf_layout", no_geometry)
    with pytest.raises(ValueError, match="마디 경계"):
        analyze_score(tone, pdf, paths)
    assert not list(paths.processed.rglob("timeline.json"))


@pytest.mark.parametrize("failure", [PermissionError("읽기 불가"), OSError("디스크 오류")])
def test_auto_does_not_hide_filesystem_errors(pdf_study, tone, monkeypatch, failure):
    paths, pdf = pdf_study

    def failed(*_args):
        raise failure

    monkeypatch.setattr("practice_player.score_analysis.run_omr", failed)
    with pytest.raises(OSError):
        analyze_score(tone, pdf, paths)
    assert not list(paths.processed.rglob("timeline.json"))


def test_auto_does_not_hide_audio_alignment_or_bad_route(pdf_study, tone, monkeypatch):
    paths, pdf = pdf_study
    same_score_xml(paths, pdf)

    def failed(*_args):
        raise ValueError("음원 정렬 실패")

    monkeypatch.setattr("practice_player.score_analysis.align_audio", failed)
    with pytest.raises(ValueError, match="음원 정렬"):
        analyze_score(tone, pdf, paths)
    with pytest.raises(ValueError, match="마디 범위"):
        analyze_score(tone, pdf, paths, route_override="1-99")
    assert not list(paths.processed.rglob("timeline.json"))


@pytest.mark.parametrize("settings", [
    {"meter_beats": 0}, {"meter_unit": 3}, {"bpm": float("nan")},
    {"lead_in": float("inf")}, {"route_override": "1;2"},
])
def test_auto_rejects_settings_before_recognition(pdf_study, tone, monkeypatch, settings):
    paths, pdf = pdf_study
    monkeypatch.setattr("practice_player.score_analysis.run_omr", forbid_notation)
    with pytest.raises(ValueError):
        analyze_score(tone, pdf, paths, **settings)


def test_auto_missing_explicit_notation_is_an_input_error(pdf_study, tone, monkeypatch):
    paths, pdf = pdf_study
    monkeypatch.setattr("practice_player.score_analysis.run_omr", forbid_notation)
    with pytest.raises(FileNotFoundError):
        analyze_score(tone, pdf, paths, musicxml=paths.score_targets / "missing.musicxml")


def test_auto_changed_source_during_failed_recognition_is_not_published(
    pdf_study, tone, monkeypatch,
):
    paths, pdf = pdf_study

    def changed(*_args):
        pdf.write_bytes(pdf.read_bytes() + b"\n% changed\n")
        omr_failure()

    monkeypatch.setattr("practice_player.score_analysis.run_omr", changed)
    with pytest.raises(ValueError, match="변경"):
        analyze_score(tone, pdf, paths)
    assert not list(paths.processed.rglob("timeline.json"))


def test_auto_checks_notation_source_before_fallback_publication(pdf_study, tone, monkeypatch):
    from practice_player import score_geometry

    paths, pdf = pdf_study
    xml = cached_omr(paths, pdf, tone, count=2)
    detect = score_geometry.detect_pdf_layout

    def changed(*args, **kwargs):
        result = detect(*args, **kwargs)
        xml.write_bytes(xml.read_bytes() + b"\n")
        return result

    monkeypatch.setattr(score_geometry, "detect_pdf_layout", changed)
    with pytest.raises(ValueError, match="변경"):
        analyze_score(tone, pdf, paths)
    assert not list(paths.processed.rglob("timeline.json"))


def test_auto_fallback_cache_rechecks_source_bytes(pdf_study, tone, monkeypatch):
    paths, pdf = pdf_study
    monkeypatch.setattr("practice_player.score_analysis.run_omr", omr_failure)
    result = analyze_score(tone, pdf, paths)
    saved = result.read_bytes()
    load = ScoreTimeline.load

    def changed(path):
        plan = load(path)
        pdf.write_bytes(pdf.read_bytes() + b"\n% changed during cached read\n")
        return plan

    monkeypatch.setattr(ScoreTimeline, "load", changed)
    monkeypatch.setattr("practice_player.score_analysis.run_omr", forbid_notation)
    with pytest.raises(ValueError, match="변경"):
        analyze_score(tone, pdf, paths)
    assert result.read_bytes() == saved


def test_auto_fallback_status_is_visible(pdf_study, tone, monkeypatch):
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication

    from practice_player.score_gui import ScorePanel

    paths, pdf = pdf_study
    monkeypatch.setattr("practice_player.score_analysis.run_omr", omr_failure)
    analyze_score(tone, pdf, paths)
    app = QApplication.instance() or QApplication([])
    panel = ScorePanel(paths)
    panel.audio, panel.source_hash = tone, hash_file(tone)
    try:
        panel.open_pdf(pdf)
        assert "음표 인식 대신 마디선·BPM으로 계산" in panel.status.text()
    finally:
        panel.view.clear()
        panel.close()
        panel.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


def test_cli_defaults_to_auto_and_keeps_explicit_modes():
    from practice_player.cli import parser

    arguments = ["score-analyze", "--audio", "song.wav", "--pdf", "song.pdf"]
    assert parser().parse_args(arguments).mode == "auto"
    for mode in ("audio", "tempo", "pdf"):
        assert parser().parse_args([*arguments, "--mode", mode]).mode == mode


@pytest.mark.parametrize("requires_review", [False, True])
def test_pdf_geometry_review_count_is_visible(pdf_study, tone, requires_review):
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication

    from practice_player.score_gui import ScorePanel

    app = QApplication.instance() or QApplication([])
    paths, pdf = pdf_study
    result = analyze_score(tone, pdf, paths, mode="pdf")
    plan = ScoreTimeline.load(result)
    plan.data["pdf_layout"]["review_required"] = ([
        {"page": 0, "system": 0, "reasons": ["마디선 후보 불일치"]},
        {"page": 0, "system": 0, "reasons": ["경계 확인 필요"]},
        {"page": 0, "system": 1, "reasons": ["경계 확인 필요"]},
    ] if requires_review else [])
    plan.save(result)
    panel = ScorePanel(paths)
    panel.audio, panel.source_hash = tone, hash_file(tone)
    try:
        panel.open_pdf(pdf)
        if requires_review:
            assert "배치 확인 필요 2줄" in panel.status.text()
        else:
            assert "배치 확인 필요" not in panel.status.text()
    finally:
        panel.view.clear()
        panel.close()
        panel.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
