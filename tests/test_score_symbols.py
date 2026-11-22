# SPDX-License-Identifier: GPL-3.0-only
"""Authored PDF text fixtures; no commercial music font or score is embedded."""

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from practice_player.score_analysis import parse_notation
from practice_player.score_geometry import detect_pdf_layout
from practice_player.score_symbols import supplement_xml, text_marks
from tests.test_score import xml_file
from tests.test_score_geometry import vector_pdf


def text_pdf(path, runs, *, family="Helvetica", mappings=()):
    """Use font metadata and Unicode mappings, without embedding font bytes."""
    writer = PdfWriter()
    page = writer.add_blank_page(width=1300, height=600)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/" + family),
    })
    if mappings:
        pairs = "\n".join(f"<{source:02X}> <{target:04X}>" for source, target in mappings)
        cmap = DecodedStreamObject()
        cmap.set_data((
            "/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
            "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
            "/CMapName /SyntheticSymbols def /CMapType 2 def\n"
            "1 begincodespacerange <00> <FF> endcodespacerange\n"
            f"{len(mappings)} beginbfchar\n{pairs}\nendbfchar\n"
            "endcmap CMapName currentdict /CMap defineresource pop end end"
        ).encode("ascii"))
        font[NameObject("/ToUnicode")] = writer._add_object(cmap)
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)}),
    })
    stream = DecodedStreamObject()
    stream.set_data("\n".join(
        f"BT /F1 {size} Tf {x} {y} Td ({label}) Tj ET" for x, y, size, label in runs
    ).encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.write(path)
    return path


@pytest.mark.parametrize("family,code,kind", [
    ("Maestro", 0x25, "segno"),
    ("ABCDEF+Maestro", 0xF025, "segno"),
    ("Maestro", 0xDE, "coda"),
    ("ABCDEF+Maestro", 0xF0DE, "coda"),
    ("Helvetica", 0x25, None),
    ("Helvetica", 0xDE, None),
    ("Helvetica", 0xF0DE, None),
    ("MaestroTimes", 0x25, None),
    ("abcdef+Maestro", 0x25, None),
])
def test_legacy_music_symbols_require_the_known_font(tmp_path, family, code, kind):
    pdf = text_pdf(tmp_path / "symbols.pdf", [(55, 350, 12, "A")],
                   family=family, mappings=[(ord("A"), code)])
    assert [mark["kind"] for mark in text_marks(pdf)] == ([kind] if kind else [])


@pytest.mark.parametrize("size,offset", [(9, 2), (12, 3), (18, 6)])
def test_command_punctuation_shares_text_origin_not_ink_center(tmp_path, size, offset):
    pdf = text_pdf(tmp_path / "command.pdf", [
        (55, 350 + offset, 9, "Other"), (520, 350, size, "D.S. al Coda"),
    ], family="Times-Bold")
    marks = text_marks(pdf)
    assert [mark["kind"] for mark in marks] == ["dalsegno"]
    assert "Coda" in marks[0]["text"]


def test_separate_text_objects_on_the_same_baseline_form_one_command(tmp_path):
    pdf = text_pdf(tmp_path / "objects.pdf", [
        (55, 350, 12, "D.S."), (82, 350, 12, "al Coda"),
    ], family="Times-Bold")
    assert [mark["kind"] for mark in text_marks(pdf)] == ["dalsegno"]


def test_nearby_lines_and_distant_columns_do_not_form_a_command(tmp_path):
    pdf = text_pdf(tmp_path / "unrelated.pdf", [
        (55, 350, 12, "D."), (67, 343, 12, "S."),
        (55, 250, 12, "D."), (110, 250, 12, "S."),
    ], family="Times-Bold")
    assert text_marks(pdf) == []


def test_font_destinations_and_punctuated_command_restore_pdf_route(tmp_path):
    xml = xml_file(tmp_path, [""] * 6)
    original = xml.read_bytes()
    pdf = vector_pdf(tmp_path / "score.pdf", [
        ((1300, 500), [(250, [40, 240, 440, 640, 840, 1040, 1240],
                       [(560, "To Coda"), (720, "D.S. al Coda")])], [5]),
    ])
    # Add the two authored symbol objects beside ordinary command text.
    symbols = text_pdf(tmp_path / "symbols.pdf", [(245, 296, 10, "%"), (845, 296, 10, "@")],
                       family="ABCDEF+Maestro", mappings=[(ord("%"), 0x25), (ord("@"), 0xF0DE)])
    writer = PdfWriter()
    page = writer.add_page(PdfReader(pdf).pages[0])
    page.merge_page(PdfReader(symbols).pages[0])
    writer.write(pdf)
    _, bars, _ = detect_pdf_layout(pdf, xml)
    document, additions, _ = supplement_xml(pdf, xml, bars)
    notation = parse_notation(xml, 1, document=document)
    assert [cue["source_index"] for cue in notation["cues"]] == [0, 1, 2, 3, 1, 2, 4, 5]
    assert {(item["measure"], item["kind"]) for item in additions} == {
        (2, "segno"), (3, "tocoda"), (4, "dalsegno"), (5, "coda"),
    }
    assert xml.read_bytes() == original


def test_pdf_command_does_not_replace_a_valid_xml_destination(tmp_path):
    xml = xml_file(tmp_path, [
        "", '<direction><sound segno="original"/></direction>',
        '<direction><sound tocoda="original-coda"/></direction>',
        '<direction><direction-type><words>D.S. al Coda</words></direction-type>'
        '<sound dalsegno="original"/></direction>',
        '<direction><sound coda="original-coda"/></direction>', "",
    ])
    pdf = vector_pdf(tmp_path / "score.pdf", [
        ((1300, 500), [(250, [40, 240, 440, 640, 840, 1040, 1240],
                       [(245, "Segno"), (560, "To Coda"),
                        (720, "D.S. al Coda"), (845, "Coda")])], [5]),
    ])
    _, bars, _ = detect_pdf_layout(pdf, xml)
    document, additions, _ = supplement_xml(pdf, xml, bars)
    assert additions == []
    notation = parse_notation(xml, 1, document=document)
    assert [cue["source_index"] for cue in notation["cues"]] == [0, 1, 2, 3, 1, 2, 4, 5]
