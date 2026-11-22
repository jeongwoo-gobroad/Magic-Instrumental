# SPDX-License-Identifier: GPL-3.0-only
import json
from hashlib import sha256

import pytest

from practice_player.cache import hash_file
from practice_player.config import Paths
from practice_player.score import ScoreTimeline, atomic_json
from practice_player.score_analysis import analyze_score
from tests.test_score import xml_file
from tests.test_score_geometry import vector_pdf


def direction(attributes):
    return (
        "<direction><direction-type><words/></direction-type>"
        f"<sound {attributes}/></direction>"
    )


@pytest.fixture
def navigation_study(tmp_path):
    paths = Paths(tmp_path / "project")
    paths.create()
    pdf = vector_pdf(
        paths.score_targets / "study.pdf",
        [((900, 600), [(350, [50, 280, 530, 850], [])], [5])] * 2,
    )
    xml = xml_file(
        paths.score_targets,
        [
            "", direction('segno="s"'), direction('tocoda="c"'),
            '<print new-page="yes"/>' + direction('dalsegno="s"'),
            direction('coda="c"'), "",
        ],
    ).rename(pdf.with_suffix(".musicxml"))
    return paths, pdf, xml


def estimated_alignment(_audio, notation, _report):
    return (
        [cue["beat"] * 0.5 for cue in notation["cues"]],
        notation["beats"] * 0.5,
        {"method": "test-alignment"},
    )


@pytest.mark.parametrize("layout_available", [True, False])
def test_valid_coda_navigation_survives_missing_camera_geometry(
    navigation_study, tone, monkeypatch, layout_available,
):
    paths, pdf, xml = navigation_study
    original = xml.read_bytes()
    monkeypatch.setattr("practice_player.score_analysis.align_audio", estimated_alignment)
    if not layout_available:
        def unavailable(*_args, **_kwargs):
            raise ValueError("Synthetic score has no reliable camera geometry")

        monkeypatch.setattr("practice_player.score_layout.analyze_layout", unavailable)
    plan = ScoreTimeline.load(analyze_score(tone, pdf, paths))
    assert [cue["source_index"] for cue in plan.cues] == [0, 1, 2, 3, 1, 2, 4, 5]
    assert [cue["page"] for cue in plan.cues] == [0, 0, 0, 1, 0, 0, 1, 1]
    assert [cue["visit"] for cue in plan.cues] == [1, 1, 1, 1, 2, 2, 1, 1]
    assert plan.data["alignment"]["method"] == "test-alignment"
    assert xml.read_bytes() == original


@pytest.mark.parametrize("problem", ["conflicting-command", "missing-segno", "missing-coda"])
def test_incomplete_navigation_does_not_publish_linear_automatic_timing(
    tmp_path, tone, monkeypatch, problem,
):
    paths = Paths(tmp_path / "project")
    paths.create()
    command = "D.C. al Coda" if problem == "conflicting-command" else "D.S. al Coda"
    pdf = vector_pdf(
        paths.score_targets / "study.pdf",
        [((1300, 500), [(250, [40, 240, 440, 640, 840, 1040, 1240],
                         [(720, command)])], [5])],
    )
    xml = xml_file(
        paths.score_targets,
        [
            "", "" if problem == "missing-segno" else direction('segno="s"'),
            direction('tocoda="c"'), direction('dalsegno="s"'),
            "" if problem == "missing-coda" else direction('coda="c"'), "",
        ],
    ).rename(pdf.with_suffix(".musicxml"))
    original = xml.read_bytes()

    def no_alignment(*_args, **_kwargs):
        pytest.fail("Incomplete navigation must be resolved before audio alignment")

    monkeypatch.setattr("practice_player.score_analysis.align_audio", no_alignment)
    with pytest.raises((ValueError, RuntimeError)):
        analyze_score(tone, pdf, paths)
    assert not list(paths.processed.rglob("timeline.json"))
    assert xml.read_bytes() == original


def legacy_fallback(audio, pdf, xml, paths, *, reviewed=True, route=""):
    audio_hash, pdf_hash = hash_file(audio), hash_file(pdf)
    signature = {
        "version": 1, "mode": "auto", "bpm": 120.0, "lead_in": 0.0,
        "meter_beats": 4, "meter_unit": 4, "route": route, "pdf_recognition": 4,
        "timing": "pdf-bars-fixed-tempo-v1",
        "automatic": {"version": 1, "notation_sha256": hash_file(xml), "omr": 2},
    }
    key = sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()[:20]
    folder = paths.processed / audio_hash / "scores" / pdf_hash
    path = folder / key / "timeline.json"
    originals = [
        {"source_index": index, "measure": str(index + 1), "page": index // 3,
         "beats": 4, "marks": []}
        for index in range(6)
    ]
    ScoreTimeline({
        "version": 1, "audio_sha256": audio_hash, "pdf_sha256": pdf_hash,
        "pdf_name": pdf.name, "page_count": 2, "signature": signature,
        "notation_source": {"method": "pdf-geometry", "version": 1},
        "originals": originals,
        "cues": [{**bar, "visit": 1, "beat": index * 4, "start": index * 2.0}
                 for index, bar in enumerate(originals)],
        "end": 12.0, "reviewed": reviewed, "offset": 0.375,
        "alignment": {"method": "pdf-fixed-tempo", "bpm": 120.0, "lead_in": 0.0,
                      "meter_beats": 4, "meter_unit": 4},
        "analysis_fallback": {"from": "notation", "to": "pdf-geometry",
                              "stage": "layout", "reason": "Legacy layout failure"},
    }).save(path)
    atomic_json(folder / "latest.json", {"analysis": key})
    return path


def test_old_automatic_fallback_is_reconsidered_without_overwriting_reviewed_file(
    navigation_study, tone, monkeypatch,
):
    paths, pdf, xml = navigation_study
    previous = legacy_fallback(tone, pdf, xml, paths)
    reviewed_bytes = previous.read_bytes()
    monkeypatch.setattr("practice_player.score_analysis.align_audio", estimated_alignment)
    result = analyze_score(tone, pdf, paths)
    plan = ScoreTimeline.load(result)
    assert result != previous
    assert [cue["source_index"] for cue in plan.cues] == [0, 1, 2, 3, 1, 2, 4, 5]
    assert plan.data["alignment"]["method"] == "test-alignment"
    assert previous.read_bytes() == reviewed_bytes


def test_full_analysis_recovers_verified_legacy_recognition_without_rerunning_omr(
    navigation_study, tone, monkeypatch,
):
    paths, pdf, xml = navigation_study
    previous = analyze_score(tone, pdf, paths, mode="tempo")
    plan = ScoreTimeline.load(previous)
    plan.data["reviewed"] = True
    plan.save(previous)
    legacy = previous.parent.parent / "recognition/score.mxl"
    legacy.parent.mkdir()
    xml.rename(legacy)
    fallback = legacy_fallback(tone, pdf, legacy, paths, reviewed=False)
    before = {path: path.read_bytes() for path in (previous, legacy, fallback)}

    def no_omr(*_args, **_kwargs):
        pytest.fail("A hash-verified previous recognition source should be reused")

    monkeypatch.setattr("practice_player.score_analysis.run_omr", no_omr)
    monkeypatch.setattr("practice_player.score_analysis.align_audio", estimated_alignment)
    result = ScoreTimeline.load(analyze_score(tone, pdf, paths))
    assert [cue["source_index"] for cue in result.cues] == [0, 1, 2, 3, 1, 2, 4, 5]
    assert result.data["xml_sha256"] == hash_file(legacy)
    assert all(path.read_bytes() == content for path, content in before.items())


@pytest.mark.parametrize("source_changed", [False, True])
def test_full_pdf_reanalysis_retains_verified_source_and_tempo_choice(
    navigation_study, tone, monkeypatch, source_changed,
):
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication, QCheckBox, QDialog

    from practice_player.score_gui import ScorePanel

    paths, pdf, xml = navigation_study
    xml = xml.rename(paths.score_targets / "reviewed-notation.musicxml")
    previous = analyze_score(tone, pdf, paths, musicxml=xml, mode="tempo", bpm=96)
    before = previous.read_bytes()
    if source_changed:
        xml.write_bytes(xml.read_bytes() + b"\n<!-- changed source -->\n")
    app = QApplication.instance() or QApplication([])
    panel = ScorePanel(paths)
    panel.audio, panel.source_hash = tone, hash_file(tone)
    captured = []
    panel.analysis_requested.connect(captured.append)

    def accept(dialog):
        reuse = next(box for box in dialog.findChildren(QCheckBox)
                     if box.text().startswith("기존 마디 시간표"))
        reuse.setChecked(False)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(QDialog, "exec", accept)
    try:
        panel.open_pdf(pdf)
        panel.analysis_options()
        options = captured[0]
        assert "base-timeline" not in options
        assert options["mode"] == "tempo"
        assert options["musicxml"] == ("" if source_changed else str(xml))
        assert options["bpm"] == 96
        assert previous.read_bytes() == before
    finally:
        panel.view.clear()
        panel.close()
        panel.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


@pytest.mark.parametrize("old_fallback,reviewed,route", [
    (True, False, ""), (True, True, ""), (True, False, "1-6"), (False, True, ""),
])
def test_pdf_analysis_dialog_rechecks_old_fallback_but_keeps_valid_reviewed_timing(
    navigation_study, tone, monkeypatch, old_fallback, reviewed, route,
):
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication, QCheckBox, QDialog

    from practice_player.score_gui import ScorePanel

    paths, pdf, xml = navigation_study
    if old_fallback:
        previous = legacy_fallback(tone, pdf, xml, paths, reviewed=reviewed, route=route)
    else:
        monkeypatch.setattr("practice_player.score_analysis.align_audio", estimated_alignment)
        previous = analyze_score(tone, pdf, paths)
        plan = ScoreTimeline.load(previous)
        plan.data.update(reviewed=True, offset=0.375)
        plan.save(previous)
    reviewed_bytes = previous.read_bytes()
    app = QApplication.instance() or QApplication([])
    panel = ScorePanel(paths)
    panel.audio, panel.source_hash = tone, hash_file(tone)
    captured = []
    panel.analysis_requested.connect(captured.append)
    keep_timing = not old_fallback or reviewed or bool(route)

    def accept(dialog):
        reuse = next(box for box in dialog.findChildren(QCheckBox)
                     if box.text().startswith("기존 마디 시간표"))
        assert reuse.isChecked() is keep_timing
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(QDialog, "exec", accept)
    try:
        panel.open_pdf(pdf)
        assert panel.timeline.data["reviewed"] is reviewed
        if not keep_timing:
            assert any(word in panel.status.text() for word in ("다시", "재분석"))
        panel.analysis_options()
        assert len(captured) == 1
        if not keep_timing:
            assert "base-timeline" not in captured[0]
        else:
            assert captured[0]["base-timeline"] == str(previous)
        assert previous.read_bytes() == reviewed_bytes
    finally:
        panel.view.clear()
        panel.close()
        panel.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
