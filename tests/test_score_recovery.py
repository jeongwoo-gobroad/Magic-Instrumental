# SPDX-License-Identifier: GPL-3.0-only
import json
import os
from copy import deepcopy

import pytest
from filelock import FileLock
from pypdf import PdfWriter

from practice_player.cache import hash_file
from practice_player.config import Paths
from practice_player.score import (
    AUTO_ANALYSIS_VERSION,
    ScoreTimeline,
    atomic_json,
    load_last_timeline,
    recover_notation_source,
    stale_pdf_fallback,
)
from tests.test_score import xml_file


@pytest.fixture
def saved_score(tmp_path, tone):
    paths = Paths(tmp_path / "project")
    paths.create()
    pdf = paths.score_targets / "study.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=600, height=800)
    with pdf.open("wb") as output:
        writer.write(output)
    xml = xml_file(paths.score_targets, [""] * 4)
    audio_hash, pdf_hash = hash_file(tone), hash_file(pdf)
    folder = paths.processed / audio_hash / "scores" / pdf_hash
    route = [0, 1, 2, 1, 2, 3]
    data = {
        "version": 1, "audio_sha256": audio_hash, "pdf_sha256": pdf_hash,
        "page_count": 1, "reviewed": True, "offset": 0.25,
        "xml_sha256": hash_file(xml), "musicxml_path": str(xml),
        "signature": {"mode": "tempo", "bpm": 120, "route": "1-3,2-4", "omr": 2},
        "cues": [{"start": 2.0 * index, "page": 0, "source_index": source}
                 for index, source in enumerate(route)],
        "end": 12.0,
    }
    return paths, pdf, xml, audio_hash, folder, data


def save_plan(folder, name, data, *, selected=False, modified=100):
    path = folder / name / "timeline.json"
    ScoreTimeline(deepcopy(data)).save(path)
    os.utime(path, (modified, modified))
    if selected:
        atomic_json(folder / "latest.json", {"analysis": name})
    return path


def obsolete_fallback(data, *, reviewed=False, route=""):
    data = deepcopy(data)
    data.update(reviewed=reviewed, notation_source={"method": "pdf-geometry"})
    data.pop("musicxml_path", None)
    data.pop("xml_sha256", None)
    data["signature"] = {"mode": "auto", "route": route, "automatic": {"version": 1}}
    data["analysis_fallback"] = {"stage": "navigation", "reason": "Synthetic old failure"}
    data["cues"] = [{"start": 2.0 * index, "page": 0, "source_index": index}
                    for index in range(4)]
    data["end"] = 8.0
    return data


def test_recovers_most_recent_reviewed_result_without_changing_timeline_files(saved_score):
    paths, pdf, _, audio_hash, folder, data = saved_score
    older = save_plan(folder, "older", data, modified=100)
    restored = save_plan(folder, "reviewed", {**data, "offset": 0.75}, modified=200)
    fallback = save_plan(folder, "fallback", obsolete_fallback(data),
                         selected=True, modified=300)
    before = {path: path.read_bytes() for path in (older, restored, fallback)}
    selected, plan = load_last_timeline(pdf, paths, audio_hash)
    assert selected == restored
    assert plan.data["offset"] == 0.75 and len(plan.cues) == 6
    assert json.loads((folder / "latest.json").read_bytes()) == {"analysis": "reviewed"}
    assert all(path.read_bytes() == content for path, content in before.items())
    assert load_last_timeline(pdf, paths, audio_hash)[0] == restored


@pytest.mark.parametrize("problem", ["wrong-audio", "wrong-pdf", "unreviewed", "fallback",
                                     "invalid-timing", "invalid-json", "nested"])
def test_unverified_previous_results_do_not_replace_fallback(saved_score, problem):
    paths, pdf, _, audio_hash, folder, data = saved_score
    candidate = deepcopy(data)
    if problem == "wrong-audio":
        candidate["audio_sha256"] = "0" * 64
    elif problem == "wrong-pdf":
        candidate["pdf_sha256"] = "0" * 64
    elif problem == "unreviewed":
        candidate["reviewed"] = False
    elif problem == "fallback":
        candidate = obsolete_fallback(candidate, reviewed=True)
    previous = save_plan(folder, "nested/previous" if problem == "nested" else "previous",
                         candidate)
    if problem == "invalid-timing":
        candidate["cues"][1]["start"] = 0
        previous.write_text(json.dumps(candidate), encoding="utf-8")
    elif problem == "invalid-json":
        previous.write_text("{broken", encoding="utf-8")
    fallback = save_plan(folder, "fallback", obsolete_fallback(data), selected=True)
    pointer = (folder / "latest.json").read_bytes()
    assert load_last_timeline(pdf, paths, audio_hash)[0] == fallback
    assert (folder / "latest.json").read_bytes() == pointer


@pytest.mark.parametrize("protection", ["reviewed", "explicit-route", "current-policy", "normal"])
def test_existing_user_selection_is_protected(saved_score, protection):
    paths, pdf, _, audio_hash, folder, data = saved_score
    save_plan(folder, "previous", data)
    selected_data = obsolete_fallback(data)
    if protection == "reviewed":
        selected_data["reviewed"] = True
    elif protection == "explicit-route":
        selected_data["signature"]["route"] = "1-3,2-4"
    elif protection == "current-policy":
        selected_data["signature"]["automatic"]["version"] = AUTO_ANALYSIS_VERSION
    else:
        selected_data = {**data, "reviewed": False}
    selected = save_plan(folder, "selected", selected_data, selected=True)
    before = selected.read_bytes()
    assert load_last_timeline(pdf, paths, audio_hash)[0] == selected
    assert selected.read_bytes() == before


def test_no_prior_result_and_missing_pointer_do_not_create_a_new_analysis(saved_score):
    paths, pdf, _, audio_hash, folder, data = saved_score
    assert load_last_timeline(pdf, paths, audio_hash) is None
    fallback = save_plan(folder, "fallback", obsolete_fallback(data), selected=True)
    assert load_last_timeline(pdf, paths, audio_hash)[0] == fallback
    assert list(folder.glob("*/timeline.json")) == [fallback]


def test_recovery_scan_is_bounded(saved_score, monkeypatch):
    from practice_player import score

    paths, pdf, _, audio_hash, folder, data = saved_score
    save_plan(folder, "previous", data)
    fallback = save_plan(folder, "fallback", obsolete_fallback(data), selected=True)
    monkeypatch.setattr(score, "_RECOVERY_SCAN_LIMIT", 1)
    assert load_last_timeline(pdf, paths, audio_hash)[0] == fallback


def test_read_only_pointer_still_allows_recovered_timeline(saved_score, monkeypatch):
    paths, pdf, _, audio_hash, folder, data = saved_score
    previous = save_plan(folder, "previous", data)
    save_plan(folder, "fallback", obsolete_fallback(data), selected=True)

    def unavailable(*_args, **_kwargs):
        raise PermissionError("Read-only cache")

    monkeypatch.setattr("practice_player.score.atomic_json", unavailable)
    assert load_last_timeline(pdf, paths, audio_hash)[0] == previous


def test_recovery_does_not_replace_an_active_analysis_pointer(saved_score):
    paths, pdf, _, audio_hash, folder, data = saved_score
    previous = save_plan(folder, "previous", data)
    save_plan(folder, "fallback", obsolete_fallback(data), selected=True)
    before = (folder / "latest.json").read_bytes()
    with FileLock(str(folder / ".analysis.lock")):
        assert load_last_timeline(pdf, paths, audio_hash)[0] == previous
        assert (folder / "latest.json").read_bytes() == before


@pytest.mark.parametrize("reviewed", [True, False])
def test_selected_notation_with_different_name_is_recovered_by_hash(saved_score, reviewed):
    paths, pdf, xml, audio_hash, folder, data = saved_score
    assert xml.stem != pdf.stem
    selected = save_plan(folder, "selected", {**data, "reviewed": reviewed}, selected=True)
    before = selected.read_bytes()
    assert recover_notation_source(pdf, paths, audio_hash) == xml.resolve()
    assert selected.read_bytes() == before
    xml.write_bytes(xml.read_bytes() + b"\n<!-- changed -->\n")
    assert recover_notation_source(pdf, paths, audio_hash) is None


@pytest.mark.parametrize("relative", ["recognition/score.mxl", "recognition/reviewed.musicxml",
                                     "recognition/v2/score.mxl"])
def test_legacy_source_requires_a_matching_selected_timeline(saved_score, relative):
    paths, pdf, xml, audio_hash, folder, data = saved_score
    legacy = folder / relative
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(xml.read_bytes())
    xml.unlink()
    assert recover_notation_source(pdf, paths, audio_hash) is None
    previous = save_plan(folder, "reviewed", data)
    fallback = save_plan(folder, "fallback", obsolete_fallback(data), selected=True)
    before = {path: path.read_bytes() for path in (previous, fallback, legacy)}
    assert recover_notation_source(pdf, paths, audio_hash) == legacy.resolve()
    assert all(path.read_bytes() == content for path, content in before.items())
    legacy.write_bytes(b"Different recognition result")
    assert recover_notation_source(pdf, paths, audio_hash) is None


@pytest.mark.parametrize("automatic", [None, 1, [], "old", {"version": 1}])
def test_stale_policy_tolerates_old_metadata_and_exempts_explicit_route(automatic):
    data = {"analysis_fallback": {"stage": "layout"},
            "signature": {"mode": "auto", "route": "", "automatic": automatic}}
    assert stale_pdf_fallback(data)
    data["reviewed"] = True
    assert stale_pdf_fallback(data)
    data["signature"]["route"] = "1-4,1-2,5-6"
    assert not stale_pdf_fallback(data)
