# SPDX-License-Identifier: GPL-3.0-only
import numpy as np
import pypdfium2
import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject
from scipy import ndimage

from practice_player.score_geometry import (
    detect_pdf_layout,
    find_staves,
    ink_mask,
    pdf_only_page_geometry,
    shared_bar_boundaries,
)
from tests.test_score import xml_file
from tests.test_score_geometry import image_pdf


def score_pdf(path, pages):
    """Authored layouts use top-down points and explicit shared system connectors."""
    writer = PdfWriter()
    for size, systems in pages:
        page = writer.add_blank_page(width=size[0], height=size[1])
        commands = ["0 G 0.8 w"]

        def line(x0, y0, x1, y1):
            commands.append(f"{x0} {size[1] - y0} m {x1} {size[1] - y1} l S")

        for system in systems:
            top, pattern, xs = system["top"], system["pattern"], system["xs"]
            step = system.get("staff_step", 70)
            for index, count in enumerate(pattern):
                first = top + index * step
                last = first + (count - 1) * 8
                for offset in range(count):
                    line(xs[0], first + offset * 8, xs[-1], first + offset * 8)
                for x in xs:
                    line(x, first, x, last)
                # A short note stem must not become a shared boundary.
                line(xs[0] + 50, first + 8, xs[0] + 50, first + 28)
            if system.get("connected", True):
                line(xs[0], top, xs[0], top + (len(pattern) - 1) * step + (pattern[-1] - 1) * 8)
            if system.get("piano_bar_spans"):
                for x in xs[1:-1]:
                    line(x, top, x, top + step + (pattern[1] - 1) * 8)
        content = DecodedStreamObject()
        content.set_data("\n".join(commands).encode())
        page[NameObject("/Contents")] = writer._add_object(content)
    writer.write(path)
    return path


def raster_copy(source, target):
    with pypdfium2.PdfDocument(source) as document:
        page = document[0]
        bitmap = page.render(scale=1.5)
        try:
            gray = np.asarray(bitmap.to_pil().convert("L"))
        finally:
            bitmap.close()
            page.close()
    return image_pdf(target, gray)


@pytest.mark.parametrize("pattern", [
    [5], [6], [4], [5, 6], [5, 5], [5, 6, 5, 6, 5, 5, 5, 5, 4, 5],
])
@pytest.mark.parametrize("raster", [False, True])
def test_solo_tab_piano_and_band_multiple_systems(tmp_path, pattern, raster):
    span = (len(pattern) - 1) * 70 + (pattern[-1] - 1) * 8
    second = 80 + span + 140
    height = second + span + 100
    pdf = score_pdf(tmp_path / "study.pdf", [((700, height), [
        dict(top=80, pattern=pattern, xs=[50, 280, 650]),
        dict(top=second, pattern=pattern, xs=[50, 230, 490]),
    ])])
    if raster:
        pdf = raster_copy(pdf, tmp_path / "scan.pdf")
    _, bars, info = detect_pdf_layout(pdf)
    assert info["method"] == "pdf-only-staves-barlines"
    assert info["pages"][0]["systems"] == 2
    assert info["pages"][0]["staff_patterns"] == [pattern, pattern]
    assert len(bars) == 4
    assert [bar["system"] for bar in bars.values()] == [0, 0, 1, 1]
    assert bars[2]["right"] < bars[1]["right"]
    assert bars[2]["top"] > bars[0]["bottom"]
    assert bars[0]["top"] < 80 / height
    assert bars[0]["bottom"] > (80 + span) / height


def test_different_staff_counts_across_pages_and_systems(tmp_path):
    pdf = score_pdf(tmp_path / "changing.pdf", [
        ((700, 800), [dict(top=70, pattern=[5], xs=[50, 300, 650]),
                      dict(top=350, pattern=[5, 6], xs=[50, 420])]),
        ((700, 800), [dict(top=80, pattern=[5, 5, 4], xs=[50, 220, 450, 650])]),
    ])
    sizes, bars, info = detect_pdf_layout(pdf)
    assert len(sizes) == 2 and len(bars) == 6
    assert [page["staff_patterns"] for page in info["pages"]] == [[[5], [5, 6]], [[5, 5, 4]]]
    assert [bar["page"] for bar in bars.values()] == [0, 0, 0, 1, 1, 1]


def test_group_spanning_barlines_share_the_same_measure_count(tmp_path):
    pdf = score_pdf(tmp_path / "piano.pdf", [((700, 700), [
        dict(top=100, pattern=[5, 5, 5], xs=[50, 230, 450, 650], piano_bar_spans=True),
    ])])
    _, bars, info = detect_pdf_layout(pdf)
    assert len(bars) == 3
    assert info["pages"][0]["staff_measure_counts"] == [[3, 3, 3]]


def test_scanned_solo_systems_are_deskewed(tmp_path):
    gray = np.full((1000, 850), 238, np.uint8)
    for top in (200, 500):
        for y in range(top, top + 49, 12):
            gray[y:y + 2, 70:780] = 155
        for x in (70, 240, 550, 778):
            gray[top:top + 50, x:x + 2] = 155
    gray = ndimage.rotate(gray, 1.2, reshape=False, order=1, cval=238)
    _, bars, info = detect_pdf_layout(image_pdf(tmp_path / "skew.pdf", gray))
    assert len(bars) == 6
    assert info["pages"][0]["systems"] == 2
    assert abs(info["pages"][0]["deskew_degrees"] + 1.2) < 0.2


def test_gap_repair_does_not_insert_a_cymbal_row_or_split_clear_staff():
    gray = np.full((300, 1000), 255, np.uint8)
    for y in range(100, 141, 10):
        gray[y:y + 2, 50:950] = 0
    for y in (90, 125):
        for x in range(200, 800, 20):
            gray[y:y + 2, x:x + 9] = 0
    staves = find_staves(ink_mask(gray), allow_four=True)
    assert len(staves) == 1
    assert len(staves[0]["lines"]) == 5
    assert staves[0]["lines"][0] == 100.5


def test_close_unconnected_rows_are_rejected_as_ambiguous(tmp_path):
    pdf = score_pdf(tmp_path / "ambiguous.pdf", [((700, 700), [
        dict(top=100, pattern=[5, 5], xs=[50, 300, 650], connected=False),
    ])])
    with pytest.raises(ValueError, match="연결선"):
        detect_pdf_layout(pdf)


def test_xml_path_still_requires_exact_measure_count(tmp_path):
    pdf = score_pdf(tmp_path / "strict.pdf", [((700, 700), [
        dict(top=100, pattern=[5], xs=[50, 300, 650]),
    ])])
    xml = xml_file(tmp_path, [""] * 3)
    with pytest.raises(ValueError, match="일치하지"):
        detect_pdf_layout(pdf, xml)


@pytest.mark.parametrize("raster", [False, True])
def test_short_landscape_single_staff(tmp_path, raster):
    pdf = score_pdf(tmp_path / "strip.pdf", [((700, 150), [
        dict(top=50, pattern=[5], xs=[50, 300, 650]),
    ])])
    if raster:
        pdf = raster_copy(pdf, tmp_path / "strip-scan.pdf")
    _, bars, info = detect_pdf_layout(pdf)
    assert len(bars) == 2
    assert info["pages"][0]["staff_patterns"] == [[5]]


def test_learned_piano_pattern_does_not_merge_distant_solo_systems(tmp_path):
    pdf = score_pdf(tmp_path / "hidden-part.pdf", [
        ((700, 800), [dict(top=100, pattern=[5, 5], xs=[50, 300, 650])]),
        ((700, 800), [dict(top=100, pattern=[5], xs=[50, 300, 650]),
                      dict(top=500, pattern=[5], xs=[50, 300, 650])]),
    ])
    _, bars, info = detect_pdf_layout(pdf)
    assert len(bars) == 6
    assert [p["systems"] for p in info["pages"]] == [1, 2]


def test_repeated_connected_pattern_can_continue_without_connectors(tmp_path):
    pdf = score_pdf(tmp_path / "paired.pdf", [
        ((700, 800), [dict(top=100, pattern=[5, 6], xs=[50, 300, 650]),
                      dict(top=400, pattern=[5, 6], xs=[50, 300, 650], connected=False)]),
        ((700, 500), [dict(top=100, pattern=[5, 6], xs=[50, 300, 650], connected=False)]),
    ])
    _, bars, info = detect_pdf_layout(pdf)
    assert len(bars) == 6
    assert [p["systems"] for p in info["pages"]] == [2, 1]
    assert [(item["page"], item["system"]) for item in info["review_required"]] == [(0, 1), (1, 0)]
    assert all(
        any("연결선 없는 보표" in reason for reason in item["reasons"])
        for item in info["review_required"]
    )
    assert info["pages"][0]["grouping"] == [
        "repeated-connected-pattern", "inferred-connected-pattern"
    ]


def test_horizontal_lines_alone_are_not_a_measure(tmp_path):
    gray = np.full((300, 1000), 255, np.uint8)
    for y in range(100, 141, 10):
        gray[y:y + 2, 50:950] = 0
    with pytest.raises(ValueError, match="실제 세로 마디선"):
        detect_pdf_layout(image_pdf(tmp_path / "empty-staff.pdf", gray))
    pdf = score_pdf(tmp_path / "one-measure.pdf", [((700, 300), [
        dict(top=100, pattern=[5], xs=[50, 650]),
    ])])
    _, bars, info = detect_pdf_layout(pdf)
    assert len(bars) == 1
    assert info["review_required"] == []


def test_notehead_is_not_a_vector_bar_but_crossing_tie_is_preserved():
    from PIL import Image, ImageDraw

    image = Image.new("L", (1000, 300), 255)
    draw = ImageDraw.Draw(image)
    for y in range(100, 141, 10):
        draw.line((50, y, 950, y), fill=0, width=2)
    vectors = []
    for x in (50, 300, 450, 600, 950):
        draw.line((x, 100, x, 140), fill=0, width=2)
        vectors.append((x, 100, 140))
    draw.ellipse((433, 133, 451, 147), fill=0)
    draw.line([(x, round(136 + 4 * ((x - 300) / 20) ** 2)) for x in range(280, 321)],
              fill=0, width=2)
    systems, _, _ = pdf_only_page_geometry(np.asarray(image), vectors)
    assert len(systems[0]["bars"]) == 3
    assert systems[0]["bars"][0]["right"] == pytest.approx(0.3, abs=0.002)


def consensus_fixture(count):
    ink = np.zeros((700, 1000), dtype=bool)
    staves = []
    for index in range(count):
        top = 50 + index * 100
        lines = list(range(top, top + 41, 10))
        ink[lines, 50:951] = True
        for x in (50, 300, 500, 700, 950):
            ink[top:top + 41, x] = True
        staves.append(dict(lines=lines, spacing=10, left=50, right=950))
    return ink, staves


def test_discarded_boundary_is_reported_for_review(monkeypatch):
    observations = iter([[50, 300, 500, 700, 950], [50, 300, 700, 950], [50, 300, 700, 950]])
    monkeypatch.setattr(
        "practice_player.score_geometry.barlines", lambda *a, **k: next(observations)
    )
    ink, staves = consensus_fixture(3)
    xs, info = shared_bar_boundaries(ink, staves, [])
    assert len(xs) == 4
    assert info["review_reasons"]
    assert any(not c["accepted"] and c["x"] == 500 for c in info["boundary_candidates"])


def test_chained_outliers_cannot_vote_for_one_shared_boundary(monkeypatch):
    observations = iter([[50, x, 950] for x in (300, 315, 330, 345, 360)])
    monkeypatch.setattr(
        "practice_player.score_geometry.barlines", lambda *a, **k: next(observations)
    )
    ink, staves = consensus_fixture(5)
    with pytest.raises(ValueError, match="충돌"):
        shared_bar_boundaries(ink, staves, [])
