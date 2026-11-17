# SPDX-License-Identifier: GPL-3.0-only
import zlib
from copy import deepcopy
from xml.etree.ElementTree import SubElement

import numpy as np
import pytest
from pypdf import PdfWriter
from pypdf.generic import (
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
)
from scipy import ndimage

from practice_player.config import Paths
from practice_player.score import ScoreTimeline
from practice_player.score_analysis import analyze_score, parse_notation
from practice_player.score_geometry import detect_pdf_layout, page_geometry
from practice_player.score_layout import analyze_scroll
from practice_player.score_symbols import supplement_xml
from practice_player.score_view import view_profile
from tests.test_score import xml_file


def vector_pdf(path, pages):
    """Synthetic score: (size, [(y, bar boundaries, text labels)], staff pattern)."""
    writer = PdfWriter()
    font = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
    )
    for size, rows, pattern in pages:
        page = writer.add_blank_page(width=size[0], height=size[1])
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        commands = ["0 G 0 g 0.8 w"]
        for y, boundaries, labels in rows:
            for staff, count in enumerate(pattern):
                bottom = y - staff * 85
                for line in range(count):
                    yy = bottom + line * 7
                    commands.append(f"{boundaries[0]} {yy} m {boundaries[-1]} {yy} l S")
                for x in boundaries:
                    commands.append(f"{x} {bottom} m {x} {bottom + (count - 1) * 7} l S")
                # A stem shorter than a staff must not become an extra measure.
                for x in boundaries[:-1]:
                    commands.append(f"{x + 35} {bottom} m {x + 35} {bottom + 24} l S")
            for x, label in labels:
                commands.append(f"BT /F1 10 Tf {x} {y + 46} Td ({label}) Tj ET")
        stream = DecodedStreamObject()
        stream.set_data("\n".join(commands).encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as output:
        writer.write(output)
    return path


def test_geometry_without_print_width_metadata_maps_mixed_sizes_and_short_system(tmp_path):
    xml = xml_file(tmp_path, [""] * 6)
    pdf = vector_pdf(
        tmp_path / "score.pdf",
        [
            ((1191, 842), [(600, [60, 250, 700, 1100], []), (400, [60, 230, 600], [])], [5]),
            ((720, 1000), [(750, [80, 650], [])], [5]),
        ],
    )
    sizes, bars, info = detect_pdf_layout(pdf, xml)
    assert sizes == [[1191, 842], [720, 1000]]
    assert [b["page"] for b in bars.values()] == [0, 0, 0, 0, 0, 1]
    assert [b["system"] for b in bars.values()] == [0, 0, 0, 1, 1, 0]
    assert bars[1]["right"] - bars[1]["left"] > 2 * (bars[0]["right"] - bars[0]["left"])
    assert info["pages"][0]["orientation"] == "landscape"
    assert info["pages"][1]["orientation"] == "portrait"


def test_piano_and_tab_staves_share_measures(tmp_path):
    xml = xml_file(tmp_path, [""] * 3)
    from defusedxml import ElementTree as ET

    root = ET.fromstring(xml.read_bytes())
    second = deepcopy(root.find("part"))
    second.set("id", "P2")
    attrs = second.find("measure/attributes")
    SubElement(SubElement(attrs, "staff-details"), "staff-lines").text = "6"
    root.append(second)
    xml.write_bytes(ET.tostring(root))
    pdf = vector_pdf(
        tmp_path / "score.pdf",
        [
            ((900, 700), [(450, [50, 250, 550, 850], [])], [5, 6]),
        ],
    )
    _, bars, _ = detect_pdf_layout(pdf, xml)
    assert len(bars) == 3
    assert bars[0]["bottom"] - bars[0]["top"] > 0.15


def image_pdf(path, gray):
    writer = PdfWriter()
    height, width = gray.shape
    page = writer.add_blank_page(width=width, height=height)
    from pypdf.generic import EncodedStreamObject

    image = EncodedStreamObject()
    image.update(
        {
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Image"),
            NameObject("/Width"): NumberObject(width),
            NameObject("/Height"): NumberObject(height),
            NameObject("/BitsPerComponent"): NumberObject(8),
            NameObject("/ColorSpace"): NameObject("/DeviceGray"),
            NameObject("/Filter"): NameObject("/FlateDecode"),
        }
    )
    image._data = zlib.compress(gray.astype(np.uint8).tobytes())
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/XObject"): DictionaryObject({NameObject("/Im"): writer._add_object(image)})}
    )
    commands = DecodedStreamObject()
    commands.set_data(f"q {width} 0 0 {height} 0 0 cm /Im Do Q".encode())
    page[NameObject("/Contents")] = writer._add_object(commands)
    with path.open("wb") as output:
        writer.write(output)
    return path


def test_scanned_faint_skewed_page_has_no_text_or_vector_metadata(tmp_path):
    gray = np.full((1000, 850), 238, dtype=np.uint8)
    for top in (200, 500):
        for y in range(top, top + 49, 12):
            gray[y : y + 2, 70:780] = 155
        for x in (70, 240, 550, 778):
            gray[top : top + 50, x : x + 2] = 155
    tilted = ndimage.rotate(gray, 1.2, reshape=False, order=1, cval=238)
    xml = xml_file(tmp_path, [""] * 6)
    pdf = image_pdf(tmp_path / "scan.pdf", tilted)
    _, bars, info = detect_pdf_layout(pdf, xml)
    assert len(bars) == 6
    assert abs(info["pages"][0]["deskew_degrees"] + 1.2) < 0.2
    assert bars[3]["top"] > bars[0]["top"]


def test_repeat_dot_pairs_are_detected_without_text(tmp_path):
    ink = np.full((600, 900), 255, dtype=np.uint8)
    top, d = 150, 14
    for y in range(top, top + 4 * d + 1, d):
        ink[y : y + 2, 50:850] = 0
    for x in (50, 450, 848):
        ink[top : top + 4 * d + 2, x : x + 2] = 0
    yy, xx = np.indices(ink.shape)
    for x in (68, 830):
        for y in (top + d * 1.5, top + d * 2.5):
            ink[(xx - x) ** 2 + (yy - y) ** 2 <= 3**2] = 0
    systems, _ = page_geometry(ink, [5])
    assert systems[0]["bars"][0]["repeat_start"]
    assert systems[0]["bars"][-1]["repeat_end"]
    pdf = image_pdf(tmp_path / "repeats.pdf", ink)
    xml = xml_file(tmp_path, ["", ""])
    _, bars, _ = detect_pdf_layout(pdf, xml)
    document, additions, _ = supplement_xml(pdf, xml, bars)
    assert {item["kind"] for item in additions} == {"forward", "backward"}
    assert [c["source_index"] for c in parse_notation(xml, 1, document=document)["cues"]] == [
        0,
        1,
        0,
        1,
    ]


def test_pdf_dc_fine_restores_route_without_rewriting_musicxml(tmp_path, tone):
    xml = xml_file(tmp_path, [""] * 4)
    original = xml.read_bytes()
    pdf = vector_pdf(
        tmp_path / "score.pdf",
        [
            (
                (1000, 500),
                [(250, [50, 250, 450, 650, 950], [(370, "Fine"), (820, "D.C. al Fine")])],
                [5],
            ),
        ],
    )
    paths = Paths(tmp_path / "project")
    plan = ScoreTimeline.load(analyze_score(tone, pdf, paths, musicxml=xml, mode="tempo"))
    assert [c["source_index"] for c in plan.cues] == [0, 1, 2, 3, 0, 1]
    assert {x["kind"] for x in plan.data["pdf_symbols_added"]} == {"fine", "dacapo"}
    assert xml.read_bytes() == original


def test_pdf_ds_coda_destinations_and_incomplete_jump(tmp_path):
    xml = xml_file(tmp_path, [""] * 6)
    pdf = vector_pdf(
        tmp_path / "score.pdf",
        [
            (
                (1300, 500),
                [
                    (
                        250,
                        [40, 240, 440, 640, 840, 1040, 1240],
                        [(245, "Segno"), (560, "To Coda"), (720, "D.S. al Coda"), (845, "Coda")],
                    )
                ],
                [5],
            ),
        ],
    )
    _, bars, _ = detect_pdf_layout(pdf, xml)
    document, additions, _ = supplement_xml(pdf, xml, bars)
    notation = parse_notation(xml, 1, document=document)
    assert [c["source_index"] for c in notation["cues"]] == [0, 1, 2, 3, 1, 2, 4, 5]
    assert len(additions) == 4
    bad = xml_file(
        tmp_path,
        ["", "<direction><direction-type><words>D.S.</words></direction-type></direction>"],
    )
    with pytest.raises(ValueError, match="세뇨"):
        parse_notation(bad, 1)


def test_new_analysis_assigns_pdf_pages_and_view_only_preserves_times(tmp_path, tone):
    xml = xml_file(tmp_path, [""] * 4)
    pdf = vector_pdf(
        tmp_path / "score.pdf",
        [
            ((800, 600), [(300, [50, 300, 750], [])], [5]),
            ((800, 600), [(300, [50, 400, 750], [])], [5]),
        ],
    )
    paths = Paths(tmp_path / "project")
    base = analyze_score(tone, pdf, paths, musicxml=xml, mode="tempo")
    plan = ScoreTimeline.load(base)
    assert [c["page"] for c in plan.cues] == [0, 0, 1, 1]
    plan.data.update(reviewed=True, offset=0.25)
    plan.save(base)
    view = analyze_scroll(tone, pdf, paths, base, view_profile(2, 700, 400))
    updated = ScoreTimeline.load(view)
    assert updated.cues == plan.cues
    assert updated.data["offset"] == 0.25 and updated.data["reviewed"]
    assert updated.data["scroll"]["layout_method"] == "pdf-staves-barlines"


def test_mismatched_count_is_rejected_instead_of_evenly_dividing(tmp_path):
    xml = xml_file(tmp_path, [""] * 7)
    pdf = vector_pdf(
        tmp_path / "score.pdf",
        [
            ((800, 600), [(300, [50, 300, 750], [])], [5]),
        ],
    )
    with pytest.raises(ValueError, match="일치하지"):
        detect_pdf_layout(pdf, xml)


def test_nested_pdf_forms_and_cropped_page_keep_real_bar_positions(tmp_path):
    from pypdf import PdfReader
    from pypdf.generic import ArrayObject, FloatObject, RectangleObject

    xml = xml_file(tmp_path, [""] * 3)
    pdf = vector_pdf(
        tmp_path / "score.pdf",
        [
            ((900, 700), [(400, [50, 250, 550, 850], [])], [5]),
        ],
    )
    writer = PdfWriter()
    page = writer.add_page(PdfReader(pdf).pages[0])
    form = DecodedStreamObject()
    form.set_data(page.get_contents().get_data())
    form.update(
        {
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Form"),
            NameObject("/BBox"): RectangleObject([0, 0, 900, 700]),
            NameObject("/Matrix"): ArrayObject([FloatObject(v) for v in [0.8, 0, 0, 0.8, 40, 50]]),
            NameObject("/Resources"): page["/Resources"],
        }
    )
    # Two nested forms exercise composition, not just the page-level transform.
    outer = DecodedStreamObject()
    outer.set_data(b"/Inner Do")
    outer.update(
        {
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Form"),
            NameObject("/BBox"): RectangleObject([0, 0, 900, 700]),
            NameObject("/Resources"): DictionaryObject(
                {
                    NameObject("/XObject"): DictionaryObject(
                        {NameObject("/Inner"): writer._add_object(form)}
                    )
                }
            ),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/XObject"): DictionaryObject(
                {NameObject("/Outer"): writer._add_object(outer)}
            )
        }
    )
    content = DecodedStreamObject()
    content.set_data(b"/Outer Do")
    page[NameObject("/Contents")] = writer._add_object(content)
    page.cropbox = RectangleObject([20, 30, 880, 680])
    transformed = tmp_path / "forms.pdf"
    writer.write(transformed)
    sizes, bars, _ = detect_pdf_layout(transformed, xml)
    assert sizes == [[860, 650]]
    assert len(bars) == 3
    assert bars[1]["left"] == pytest.approx((250 * 0.8 + 40 - 20) / 860, abs=0.002)


def test_rotated_pdf_coordinate_system_preserves_geometry_and_symbols(tmp_path):
    from pypdf import PdfReader
    from pypdf.generic import RectangleObject

    xml = xml_file(tmp_path, [""] * 4)
    pdf = vector_pdf(
        tmp_path / "score.pdf",
        [
            (
                (1000, 500),
                [(250, [50, 250, 450, 650, 950], [(370, "Fine"), (820, "D.C. al Fine")])],
                [5],
            ),
        ],
    )
    writer = PdfWriter()
    page = writer.add_page(PdfReader(pdf).pages[0])
    content = DecodedStreamObject()
    content.set_data(b"q 0 1 -1 0 500 0 cm\n" + page.get_contents().get_data() + b"\nQ")
    page[NameObject("/Contents")] = writer._add_object(content)
    page.mediabox = RectangleObject([0, 0, 500, 1000])
    page.rotate(90)
    rotated = tmp_path / "rotated.pdf"
    writer.write(rotated)
    sizes, bars, _ = detect_pdf_layout(rotated, xml)
    assert sizes == [[1000, 500]]
    assert bars[1]["left"] == pytest.approx(0.25, abs=0.002)
    document, additions, _ = supplement_xml(rotated, xml, bars)
    assert len(additions) == 2
    assert [c["source_index"] for c in parse_notation(xml, 1, document=document)["cues"]] == [
        0,
        1,
        2,
        3,
        0,
        1,
    ]


def test_conflicting_pdf_jump_is_not_silently_replaced(tmp_path):
    xml = xml_file(tmp_path, ["", '<direction><sound dalsegno="yes"/></direction>'])
    pdf = vector_pdf(
        tmp_path / "score.pdf",
        [
            ((800, 600), [(300, [50, 400, 750], [(650, "D.C.")])], [5]),
        ],
    )
    _, bars, _ = detect_pdf_layout(pdf, xml)
    with pytest.raises(ValueError, match="충돌"):
        supplement_xml(pdf, xml, bars)


def test_unrelated_text_and_ambiguous_symbol_location_are_not_commands():
    from practice_player.score_symbols import locate_mark, word_marks

    assert word_marks("D string, crescendo, DCmotor, defined") == []
    assert [x[0] for x in word_marks("D.S. al Coda")] == ["dalsegno"]
    assert [x[0] for x in word_marks("\U0001d10b \U0001d10c")] == ["segno", "coda"]
    bars = {
        0: dict(page=0, system=0, left=0.1, right=0.9, top=0.1, bottom=0.2),
        1: dict(page=0, system=1, left=0.1, right=0.9, top=0.25, bottom=0.35),
    }
    assert locate_mark(dict(page=0, kind="fine", x=0.5, y=0.225), bars) is None
