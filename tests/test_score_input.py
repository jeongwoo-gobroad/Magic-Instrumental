# SPDX-License-Identifier: GPL-3.0-only
import json

import numpy as np
import pytest
from PIL import Image

from practice_player.cache import hash_file
from practice_player.config import Paths
from practice_player.score import ScoreTimeline
from practice_player.score_analysis import analyze_score
from practice_player.score_geometry import find_staves, ink_mask, page_geometry
from practice_player.score_input import OMR_VERSION, prepare_omr_input
from tests.test_score import xml_file
from tests.test_score_geometry import vector_pdf


def test_private_input_removes_only_paired_tab_and_keeps_all_pages(tmp_path):
    pdf = vector_pdf(tmp_path / "mixed.pdf", [
        ((600, 800), [(600, [50, 250, 550], []), (350, [50, 550], [])], [5, 6]),
        ((600, 800), [(600, [50, 550], [])], [5, 6]),
    ])
    before = hash_file(pdf)
    result, info = prepare_omr_input(pdf, tmp_path / "private", lambda *_: None)
    assert result.suffix == ".tiff" and info["pages"] == 2
    with Image.open(result) as image:
        assert image.n_frames == 2
        for number, expected in enumerate([2, 1]):
            image.seek(number)
            staves = find_staves(ink_mask(np.asarray(image.convert("L"))))
            assert [len(s["lines"]) for s in staves] == [5] * expected
    assert hash_file(pdf) == before


def test_normal_pdf_is_used_unchanged_and_inconsistent_pages_rejected(tmp_path):
    normal = ((600, 800), [(600, [50, 550], [])], [5])
    mixed = ((600, 800), [(600, [50, 550], [])], [5, 6])
    pdf = vector_pdf(tmp_path / "normal.pdf", [normal])
    result, _ = prepare_omr_input(pdf, tmp_path / "normal-input", lambda *_: None)
    assert result == pdf
    pdf = vector_pdf(tmp_path / "inconsistent.pdf", [normal, mixed])
    with pytest.raises(ValueError, match="페이지마다"):
        prepare_omr_input(pdf, tmp_path / "bad-input", lambda *_: None)


def test_tab_digit_gaps_do_not_truncate_staff_and_ledger_is_not_sixth_line():
    gray = np.full((800, 1000), 255, np.uint8)
    for top, count in [(160, 5), (330, 6)]:
        for y in range(top, top + count * 14, 14):
            gray[y:y + 2, 50:950] = 0
        for x in (50, 350, 650, 948):
            gray[top:top + (count - 1) * 14 + 2, x:x + 2] = 0
    for y in (372, 386, 400):
        for x in range(100, 900, 45):
            gray[y - 3:y + 5, x:x + 11] = 255  # white behind fret numbers
    gray[230:232, 100:900] = 0  # long ledger below the standard staff
    staves = find_staves(ink_mask(gray))
    assert [len(s["lines"]) for s in staves] == [5, 6]
    assert staves[1]["left"] < 60 and staves[1]["right"] > 940
    systems, _ = page_geometry(gray, [5])
    assert len(systems) == 1 and len(systems[0]["bars"]) == 3


def test_legacy_omr_cache_is_not_reused_and_new_cache_skips_omr(tmp_path, tone, monkeypatch):
    paths = Paths(tmp_path / "project")
    paths.create()
    pdf = vector_pdf(paths.score_targets / "study.pdf", [
        ((600, 800), [(600, [50, 300, 550], [])], [5, 6]),
    ])
    folder = paths.processed / hash_file(tone) / "scores" / hash_file(pdf) / "recognition"
    folder.mkdir(parents=True)
    legacy = folder / "score.mxl"
    legacy.write_bytes(b"invalid legacy recognition")
    xml = xml_file(tmp_path, ["", ""])
    calls = []

    def omr(_pdf, output, _report):
        calls.append(output)
        output.mkdir(parents=True)
        (output / "input.json").write_text(json.dumps({
            "method": "standard-staves-from-mixed-pdf", "version": OMR_VERSION,
        }))
        return xml

    monkeypatch.setattr("practice_player.score_analysis.run_omr", omr)
    first = analyze_score(tone, pdf, paths, mode="tempo")
    second = analyze_score(tone, pdf, paths, mode="tempo")
    assert first == second and len(calls) == 1
    assert len(ScoreTimeline.load(first).cues) == 2
    assert legacy.read_bytes() == b"invalid legacy recognition"
