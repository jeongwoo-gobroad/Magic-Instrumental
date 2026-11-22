# SPDX-License-Identifier: GPL-3.0-only
import json
import subprocess
import sys
from pathlib import Path

import pytest
from defusedxml import ElementTree as ET
from pypdf import PdfReader, PdfWriter

from practice_player.cache import hash_file
from practice_player.config import Paths
from practice_player.score import ScoreTimeline
from practice_player.score_analysis import analyze_score
from practice_player.score_layout import analyze_scroll
from practice_player.score_view import view_profile
from tests.test_pdf_only_analysis import cached_omr, estimated_alignment, score_pdf
from tests.test_score import xml_file


@pytest.fixture
def study(tmp_path):
    paths = Paths(tmp_path / "project")
    paths.create()
    return paths, score_pdf(paths.score_targets / "study.pdf")


def forbid_fallback(*_args, **_kwargs):
    pytest.fail("Existing notation must not be replaced by PDF-only timing")


@pytest.mark.parametrize("selection", ["explicit", "matching", "embedded"])
def test_existing_musicxml_is_used_before_omr_and_geometry_fallback(
    study, tone, monkeypatch, selection,
):
    paths, pdf = study
    xml = xml_file(paths.score_targets, [""] * 6)
    options = {}
    if selection == "explicit":
        options["musicxml"] = xml
    elif selection == "matching":
        xml = xml.rename(pdf.with_suffix(".musicxml"))
    else:
        writer = PdfWriter(clone_from=PdfReader(pdf))
        writer.add_attachment("printed-score.musicxml", xml.read_bytes())
        with pdf.open("wb") as output:
            writer.write(output)
    original = pdf.read_bytes()
    monkeypatch.setattr("practice_player.score_analysis.run_omr", forbid_fallback)
    monkeypatch.setattr("practice_player.score_analysis._analyze_pdf_timing", forbid_fallback)
    monkeypatch.setattr("practice_player.score_analysis.align_audio", estimated_alignment)
    result = analyze_score(tone, pdf, paths, **options)
    plan = ScoreTimeline.load(result)
    assert plan.data["notation_selection"] == selection
    assert plan.data["xml_sha256"] == hash_file(xml)
    assert len(plan.cues) == 6 and "analysis_fallback" not in plan.data
    assert plan.data["alignment"]["method"] == "test-note-alignment"
    viewed = ScoreTimeline.load(analyze_scroll(
        tone, pdf, paths, result, view_profile(2, 640, 420),
    ))
    assert len(viewed.data["scroll"]["frames"]) == 6
    assert pdf.read_bytes() == original


@pytest.mark.parametrize("explicit", [True, False])
def test_invalid_supplied_notation_is_not_replaced_by_printed_order(
    study, tone, monkeypatch, explicit,
):
    paths, pdf = study
    xml = paths.score_targets / ("selected.musicxml" if explicit else "study.musicxml")
    xml.write_text("<score-partwise><broken>", encoding="utf-8")
    before = xml.read_bytes()
    monkeypatch.setattr("practice_player.score_analysis.run_omr", forbid_fallback)
    monkeypatch.setattr("practice_player.score_analysis._analyze_pdf_timing", forbid_fallback)
    with pytest.raises((ValueError, ET.ParseError)):
        analyze_score(tone, pdf, paths, musicxml=xml if explicit else None)
    assert xml.read_bytes() == before
    assert not list(paths.processed.rglob("timeline.json"))


def test_notation_mismatch_keeps_notes_and_does_not_invent_printed_timing(
    study, tone, monkeypatch,
):
    paths, pdf = study
    xml = xml_file(paths.score_targets, [""] * 2)
    monkeypatch.setattr("practice_player.score_analysis._analyze_pdf_timing", forbid_fallback)
    monkeypatch.setattr("practice_player.score_analysis.align_audio", estimated_alignment)
    timing = analyze_score(tone, pdf, paths, musicxml=xml)
    plan = ScoreTimeline.load(timing)
    assert len(plan.cues) == 2 and "analysis_fallback" not in plan.data
    with pytest.raises(ValueError):
        analyze_scroll(tone, pdf, paths, timing, view_profile("page", 640, 420))


def test_new_reviewed_notation_precedes_previous_generated_omr(study, tone, monkeypatch):
    paths, pdf = study
    cached_omr(paths, pdf, tone)
    previous = analyze_score(tone, pdf, paths, mode="tempo")
    before = previous.read_bytes()
    reviewed = xml_file(paths.score_targets, [
        "", '<barline location="left"><repeat direction="forward"/></barline>', "",
        '<barline location="right"><repeat direction="backward"/></barline>', "", "",
    ]).rename(previous.parent.parent / "recognition/reviewed.musicxml")
    monkeypatch.setattr("practice_player.score_analysis.run_omr", forbid_fallback)
    monkeypatch.setattr("practice_player.score_analysis._analyze_pdf_timing", forbid_fallback)
    result = analyze_score(tone, pdf, paths, mode="tempo")
    plan = ScoreTimeline.load(result)
    assert plan.data["notation_selection"] == "reviewed"
    assert plan.data["xml_sha256"] == hash_file(reviewed)
    assert [cue["source_index"] for cue in plan.cues] == [0, 1, 2, 3, 1, 2, 3, 4, 5]
    assert previous.read_bytes() == before


def test_failed_combined_worker_preserves_previous_reviewed_scroll(study, tone):
    paths, pdf = study
    previous = analyze_score(tone, pdf, paths, mode="pdf")
    previous = analyze_scroll(tone, pdf, paths, previous, view_profile("page", 640, 420))
    plan = ScoreTimeline.load(previous)
    plan.data.update(reviewed=True, offset=0.25)
    plan.save(previous)
    pointer = previous.parent.parent / "latest.json"
    original_pointer, original_plan = pointer.read_bytes(), previous.read_bytes()
    incompatible = xml_file(paths.score_targets, [""] * 2)
    root = Path(__file__).resolve().parents[1]
    run = subprocess.run(
        [sys.executable, str(root / "main.py"), "score-analyze", "--root", str(paths.root),
         "--audio", str(tone), "--pdf", str(pdf), "--musicxml", str(incompatible),
         "--mode", "tempo", "--zoom", "page", "--view-width", "640", "--view-height", "420",
         "--events"],
        cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert run.returncode != 0
    events = [json.loads(line[len("@@PRACTICE@@"):]) for line in run.stdout.splitlines()
              if line.startswith("@@PRACTICE@@")]
    assert not any(event["type"] == "score_result" for event in events)
    assert pointer.read_bytes() == original_pointer
    assert previous.read_bytes() == original_plan


def test_gui_selected_xml_and_timing_mode_reach_worker(study, tone, monkeypatch):
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication, QComboBox, QDialog, QLineEdit

    from practice_player.score_gui import ScorePanel

    paths, pdf = study
    xml = xml_file(paths.score_targets, [""] * 6)
    app = QApplication.instance() or QApplication([])
    panel = ScorePanel(paths)
    panel.audio, panel.source_hash = tone, hash_file(tone)
    panel.open_pdf(pdf)
    captured = []
    panel.analysis_requested.connect(captured.append)

    def accept(dialog):
        source = dialog.findChild(QLineEdit, "scoreMusicXML")
        mode = dialog.findChild(QComboBox, "scoreAlignmentMode")
        assert source.isEnabled() and mode.isEnabled()
        source.setText(str(xml))
        mode.setCurrentIndex(mode.findData("tempo"))
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(QDialog, "exec", accept)
    try:
        panel.analysis_options()
        assert captured[0]["musicxml"] == str(xml)
        assert captured[0]["mode"] == "tempo"
    finally:
        panel.view.clear()
        panel.close()
        panel.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


@pytest.mark.parametrize("previous_source", [
    "generated", "generated-prefilled", "legacy-generated", "selected",
])
def test_gui_new_reviewed_source_precedes_generated_cache_but_keeps_selected_xml(
    study, tone, monkeypatch, previous_source,
):
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication, QCheckBox, QDialog, QLineEdit

    from practice_player.score_gui import ScorePanel

    paths, pdf = study
    source = cached_omr(paths, pdf, tone)
    if previous_source == "selected":
        source = source.rename(paths.score_targets / "chosen-notation.musicxml")
    previous = analyze_score(
        tone, pdf, paths, mode="tempo", bpm=96,
        musicxml=source if previous_source in {"selected", "generated-prefilled"} else None,
    )
    if previous_source == "legacy-generated":
        source = source.rename(previous.parent.parent / "recognition/score.mxl")
        plan = ScoreTimeline.load(previous)
        plan.data["musicxml_path"] = str(source)
        plan.save(previous)
    reviewed = xml_file(paths.score_targets, [
        "", '<barline location="left"><repeat direction="forward"/></barline>', "",
        '<barline location="right"><repeat direction="backward"/></barline>', "", "",
    ]).rename(previous.parent.parent / "recognition/reviewed.musicxml")
    original = {path: path.read_bytes() for path in (previous, source, reviewed)}
    app = QApplication.instance() or QApplication([])
    panel = ScorePanel(paths)
    panel.audio, panel.source_hash = tone, hash_file(tone)
    captured = []
    panel.analysis_requested.connect(captured.append)

    def accept(dialog):
        reuse = next(box for box in dialog.findChildren(QCheckBox)
                     if box.text().startswith("기존 마디 시간표"))
        reuse.setChecked(False)
        field = dialog.findChild(QLineEdit, "scoreMusicXML")
        assert field.text() == (str(source) if previous_source == "selected" else "")
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(QDialog, "exec", accept)
    monkeypatch.setattr("practice_player.score_analysis.run_omr", forbid_fallback)
    monkeypatch.setattr("practice_player.score_analysis._analyze_pdf_timing", forbid_fallback)
    try:
        panel.open_pdf(pdf)
        panel.analysis_options()
        options = captured[0]
        assert "base-timeline" not in options
        assert options["mode"] == "tempo" and options["bpm"] == 96
        result = analyze_score(
            tone, pdf, paths, mode=options["mode"], bpm=options["bpm"],
            musicxml=Path(options["musicxml"]) if options["musicxml"] else None,
        )
        plan = ScoreTimeline.load(result)
        expected_source = source if previous_source == "selected" else reviewed
        assert plan.data["xml_sha256"] == hash_file(expected_source)
        expected_route = ([0, 1, 2, 3, 4, 5] if previous_source == "selected"
                          else [0, 1, 2, 3, 1, 2, 3, 4, 5])
        assert [cue["source_index"] for cue in plan.cues] == expected_route
        assert all(path.read_bytes() == data for path, data in original.items())
    finally:
        panel.view.clear()
        panel.close()
        panel.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
