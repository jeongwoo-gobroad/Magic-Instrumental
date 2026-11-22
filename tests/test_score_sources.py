# SPDX-License-Identifier: GPL-3.0-only
"""Generated PDF attachment containers; no external scores or font files."""

from hashlib import sha256
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from defusedxml.common import DefusedXmlException
from pypdf import PdfReader, PdfWriter, get_configuration
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NullObject,
    TextStringObject,
)

from practice_player import score_sources
from practice_player.score_sources import embedded_musicxml, read_musicxml_bytes
from tests.test_score import xml_file


def mxl_bytes(document, *, container=None, root="score.musicxml"):
    result = BytesIO()
    with ZipFile(result, "w", compression=ZIP_DEFLATED) as archive:
        if container is None:
            container = (
                '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                f'<rootfiles><rootfile full-path="{root}" '
                'media-type="application/vnd.recordare.musicxml+xml"/></rootfiles></container>'
            ).encode()
        archive.writestr("META-INF/container.xml", container)
        archive.writestr(root, document)
    return result.getvalue()


def attached_pdf(tmp_path, attachments, *, mutate=None):
    writer = PdfWriter()
    writer.add_blank_page(width=600, height=800)
    entries = [writer.add_attachment(name, document) for name, document in attachments]
    if mutate is not None:
        mutate(writer, entries)
    path = tmp_path / "attached.pdf"
    writer.write(path)
    return path, PdfReader(path)


@pytest.fixture
def score_document(tmp_path):
    return xml_file(tmp_path, ["", ""]).read_bytes()


def test_plain_namespaced_xml_and_mxl_keep_original_bytes(score_document):
    document = score_document.replace(b'<score-partwise version="4.0">',
                                      b'<score-partwise xmlns="urn:musicxml" version="4.0">')
    assert read_musicxml_bytes(document) == document
    assert read_musicxml_bytes(mxl_bytes(document)) == document


def test_external_doctype_is_not_fetched(score_document):
    doctype = b'<!DOCTYPE score-partwise SYSTEM "https://invalid.example/score.dtd">'
    document = doctype + score_document
    assert read_musicxml_bytes(document) == document


def test_entities_are_rejected_before_xml_consumers():
    document = (b'<!DOCTYPE score-partwise [<!ENTITY note "C">]>'
                b'<score-partwise>&note;</score-partwise>')
    with pytest.raises(DefusedXmlException):
        read_musicxml_bytes(document)


def test_xml_and_inflated_mxl_are_bounded(monkeypatch):
    monkeypatch.setattr(score_sources, "MUSICXML_LIMIT", 2048)
    document = b"<score-partwise>" + b" " * 4096 + b"</score-partwise>"
    with pytest.raises(ValueError):
        read_musicxml_bytes(document)
    compressed = mxl_bytes(document)
    assert len(compressed) < 2048
    with pytest.raises(ValueError):
        read_musicxml_bytes(compressed)


def test_mxl_container_is_bounded(score_document):
    container = b"<container>" + b" " * 65536 + b"</container>"
    with pytest.raises(ValueError):
        read_musicxml_bytes(mxl_bytes(score_document, container=container))


def test_watermark_xml_and_metadata_are_not_notation(tmp_path, score_document):
    def metadata(writer, entries):
        stream = DecodedStreamObject()
        stream.set_data(score_document)
        stream[NameObject("/Type")] = NameObject("/Metadata")
        writer.root_object[NameObject("/Metadata")] = writer._add_object(stream)

    pdf, reader = attached_pdf(tmp_path, [("settings.xml", b'<WatermarkSettings version="8.0"/>')],
                               mutate=metadata)
    before = pdf.read_bytes()
    folder = tmp_path / "processed"
    assert embedded_musicxml(reader, folder) is None
    assert not folder.exists()
    assert pdf.read_bytes() == before


def test_attachment_is_staged_by_content_not_its_filename(tmp_path, score_document):
    pdf, reader = attached_pdf(tmp_path, [("../../outside.musicxml", score_document)])
    before = pdf.read_bytes()
    folder = tmp_path / "processed"
    result = embedded_musicxml(reader, folder)
    assert result == folder / "notation" / f"embedded-{sha256(score_document).hexdigest()}.musicxml"
    assert result.read_bytes() == score_document
    assert not (tmp_path / "outside.musicxml").exists()
    assert pdf.read_bytes() == before
    assert embedded_musicxml(reader, folder) == result
    assert list(result.parent.iterdir()) == [result]


def test_mxl_inside_flate_pdf_stream_is_read_without_extraction(tmp_path, score_document):
    def compress(writer, entries):
        files = entries[0].pdf_object["/EF"]
        files[NameObject("/F")] = writer._add_object(files["/F"].flate_encode())

    _, reader = attached_pdf(tmp_path, [("score.mxl", mxl_bytes(score_document))], mutate=compress)
    result = embedded_musicxml(reader, tmp_path / "processed")
    assert result.read_bytes() == score_document
    assert not (tmp_path / "META-INF").exists()


def test_duplicate_plain_and_compressed_copies_are_one_score(tmp_path, score_document):
    _, reader = attached_pdf(tmp_path, [("first.musicxml", score_document),
                                       ("second.mxl", mxl_bytes(score_document))])
    result = embedded_musicxml(reader, tmp_path / "processed")
    assert result.read_bytes() == score_document


def test_distinct_embedded_scores_require_source_selection(tmp_path, score_document):
    different = score_document.replace(b"<step>C</step>", b"<step>D</step>")
    _, reader = attached_pdf(tmp_path, [("first.musicxml", score_document),
                                       ("second.musicxml", different)])
    folder = tmp_path / "processed"
    with pytest.raises(ValueError):
        embedded_musicxml(reader, folder)
    assert not folder.exists()


@pytest.mark.parametrize("name,document", [
    ("score.musicxml", b"broken"),
    ("score.mxl", b"PK\x03\x04broken"),
    ("score.musicxml", b"<WatermarkSettings/>"),
    ("score.xml", b"<score-partwise>broken"),
    ("score.musicxml", b"<score-timewise/>"),
    ("score.musicxml", b'<!DOCTYPE score-partwise [<!ENTITY n "C">]>'
     b'<score-partwise>&n;</score-partwise>'),
])
def test_declared_or_identifiable_malformed_score_is_not_ignored(tmp_path, name, document):
    _, reader = attached_pdf(tmp_path, [(name, document)])
    with pytest.raises(ValueError):
        embedded_musicxml(reader, tmp_path / "processed")


@pytest.mark.parametrize("location", ["catalog", "page", "annotation"])
def test_associated_files_and_annotation_attachments(tmp_path, score_document, location):
    document = score_document.replace(b'<score-partwise version="4.0">',
                                      b'<score-partwise xmlns="urn:musicxml" version="4.0">')

    def associate(writer, entries):
        spec = entries[0].pdf_object.indirect_reference
        del writer.root_object["/Names"]
        if location == "catalog":
            writer.root_object[NameObject("/AF")] = ArrayObject([spec])
        elif location == "page":
            writer.pages[0][NameObject("/AF")] = ArrayObject([spec])
        else:
            annotation = DictionaryObject({
                NameObject("/Type"): NameObject("/Annot"),
                NameObject("/Subtype"): NameObject("/FileAttachment"),
                NameObject("/FS"): spec,
            })
            writer.pages[0][NameObject("/Annots")] = ArrayObject([writer._add_object(annotation)])

    _, reader = attached_pdf(tmp_path, [("score.musicxml", document)], mutate=associate)
    assert embedded_musicxml(reader, tmp_path / "processed").read_bytes() == document


def test_same_file_spec_in_name_tree_and_associated_list_is_not_ambiguous(tmp_path, score_document):
    def associate(writer, entries):
        spec = entries[0].pdf_object.indirect_reference
        writer.root_object[NameObject("/AF")] = ArrayObject([spec])

    _, reader = attached_pdf(tmp_path, [("score.musicxml", score_document)], mutate=associate)
    assert embedded_musicxml(reader, tmp_path / "processed").read_bytes() == score_document


def test_nested_embedded_file_name_tree(tmp_path, score_document):
    def nest(writer, entries):
        names = writer.root_object["/Names"]
        leaf = names["/EmbeddedFiles"]
        for _ in range(3):
            leaf = DictionaryObject({NameObject("/Kids"): ArrayObject([writer._add_object(leaf)])})
        names[NameObject("/EmbeddedFiles")] = writer._add_object(leaf)

    _, reader = attached_pdf(tmp_path, [("score.musicxml", score_document)], mutate=nest)
    assert embedded_musicxml(reader, tmp_path / "processed").read_bytes() == score_document


def test_cyclic_attachment_name_tree_is_rejected(tmp_path):
    def cycle(writer, entries):
        node = DictionaryObject()
        reference = writer._add_object(node)
        node[NameObject("/Kids")] = ArrayObject([reference])
        writer.root_object[NameObject("/Names")] = DictionaryObject({
            NameObject("/EmbeddedFiles"): reference,
        })

    _, reader = attached_pdf(tmp_path, [], mutate=cycle)
    with pytest.raises(ValueError):
        embedded_musicxml(reader, tmp_path / "processed")


def test_pdf_null_entries_are_equivalent_to_absent_optional_values(tmp_path, score_document):
    def nulls(writer, entries):
        writer.pages[0][NameObject("/AF")] = NullObject()
        writer.pages[0][NameObject("/Annots")] = NullObject()
        files = entries[0].pdf_object["/EF"]
        files[NameObject("/UF")] = NullObject()
        files["/F"][NameObject("/Filter")] = NullObject()

    _, reader = attached_pdf(tmp_path, [("score.musicxml", score_document)], mutate=nulls)
    assert embedded_musicxml(reader, tmp_path / "processed").read_bytes() == score_document


def test_declared_score_without_embedded_stream_fails(tmp_path):
    def absent(writer, entries):
        del entries[0].pdf_object["/EF"]

    _, reader = attached_pdf(tmp_path, [("score.musicxml", b"unused")], mutate=absent)
    with pytest.raises(ValueError):
        embedded_musicxml(reader, tmp_path / "processed")


def test_musicxml_content_and_mime_work_without_a_musicxml_filename(tmp_path, score_document):
    _, reader = attached_pdf(tmp_path, [("unknown.bin", score_document)])
    assert embedded_musicxml(reader, tmp_path / "processed").read_bytes() == score_document

    def mime(writer, entries):
        entries[0].subtype = NameObject("/application/vnd.recordare.musicxml+xml")

    _, reader = attached_pdf(tmp_path, [("unknown.bin", b"invalid")], mutate=mime)
    with pytest.raises(ValueError):
        embedded_musicxml(reader, tmp_path / "different")


def test_external_file_url_is_not_an_embedded_source(tmp_path):
    def external(writer, entries):
        spec = DictionaryObject({
            NameObject("/Type"): NameObject("/Filespec"),
            NameObject("/FS"): NameObject("/URL"),
            NameObject("/F"): TextStringObject("https://invalid.example/score.musicxml"),
        })
        writer.root_object[NameObject("/AF")] = ArrayObject([writer._add_object(spec)])

    _, reader = attached_pdf(tmp_path, [], mutate=external)
    assert embedded_musicxml(reader, tmp_path / "processed") is None


def test_attachment_count_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(score_sources, "MAX_EMBEDDED_FILES", 2)
    _, reader = attached_pdf(tmp_path, [(f"text-{index}.txt", b"plain") for index in range(3)])
    with pytest.raises(ValueError):
        embedded_musicxml(reader, tmp_path / "processed")


def test_pdf_stream_inflation_is_bounded_without_changing_global_limits(tmp_path, monkeypatch):
    def compress(writer, entries):
        files = entries[0].pdf_object["/EF"]
        files[NameObject("/F")] = writer._add_object(files["/F"].flate_encode())

    _, reader = attached_pdf(tmp_path, [("score.musicxml", b" " * 8192)], mutate=compress)
    monkeypatch.setattr(score_sources, "MUSICXML_LIMIT", 1024)
    original = get_configuration()
    with pytest.raises(ValueError):
        embedded_musicxml(reader, tmp_path / "processed")
    assert get_configuration() == original


def test_total_attachment_bytes_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(score_sources, "MUSICXML_LIMIT", 1024)
    _, reader = attached_pdf(tmp_path, [(f"text-{index}.txt", b"text" * 200) for index in range(3)])
    with pytest.raises(ValueError):
        embedded_musicxml(reader, tmp_path / "processed")
