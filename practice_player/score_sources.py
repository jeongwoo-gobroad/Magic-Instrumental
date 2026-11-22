# SPDX-License-Identifier: GPL-3.0-only
"""Bounded notation sources from bytes and genuine PDF file attachments."""

import os
import re
import uuid
import zipfile
from hashlib import sha256
from io import BytesIO
from pathlib import Path

from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException
from pypdf import apply_configuration
from pypdf.errors import PyPdfError
from pypdf.generic import ArrayObject, DictionaryObject, NullObject, StreamObject

MUSICXML_LIMIT = 32 * 1024 * 1024
MAX_EMBEDDED_FILES = 32
_SCORE_MIME_TYPES = {
    "/application/vnd.recordare.musicxml", "/application/vnd.recordare.musicxml+xml",
}
_TEXT_FILTERS = {"/FlateDecode", "/Fl", "/LZWDecode", "/LZW", "/ASCII85Decode", "/A85",
                 "/ASCIIHexDecode", "/AHx", "/RunLengthDecode", "/RL", "/BrotliDecode"}


def read_musicxml_bytes(data: bytes) -> bytes:
    """Validate XML or read its bounded MXL root without extracting archive paths."""
    if len(data) > MUSICXML_LIMIT:
        raise ValueError("MusicXML이 너무 큽니다 (최대 32 MB).")
    source = BytesIO(data)
    if zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as archive:
            infos = archive.infolist()
            if len(infos) > 1024:
                raise ValueError("MusicXML 압축 파일에 항목이 너무 많습니다.")
            if len({info.filename for info in infos}) != len(infos):
                raise ValueError("MusicXML 압축 파일에 같은 이름의 항목이 여러 개 있습니다.")
            container_info = archive.getinfo("META-INF/container.xml")
            if container_info.file_size > 65536:
                raise ValueError("잘못된 MusicXML 컨테이너입니다.")
            with archive.open(container_info) as entry:
                container_data = entry.read(65537)
            if len(container_data) > 65536:
                raise ValueError("잘못된 MusicXML 컨테이너입니다.")
            container = ET.fromstring(container_data)
            rootfile = next(x for x in container.iter() if x.tag.split("}")[-1] == "rootfile")
            info = archive.getinfo(rootfile.attrib["full-path"])
            if info.file_size > MUSICXML_LIMIT:
                raise ValueError("압축 해제한 MusicXML이 너무 큽니다.")
            with archive.open(info) as entry:
                data = entry.read(MUSICXML_LIMIT + 1)
            if len(data) > MUSICXML_LIMIT:
                raise ValueError("압축 해제한 MusicXML이 너무 큽니다.")
    # External DTD declarations are common in MusicXML; defusedxml never loads them.
    ET.fromstring(data)
    return data


def _object(value):
    value = value.get_object() if hasattr(value, "get_object") else value
    return None if isinstance(value, NullObject) else value


def _array(value):
    value = _object(value)
    if value is None:
        return ArrayObject()
    if not isinstance(value, ArrayObject):
        raise ValueError("PDF 첨부파일 목록이 손상되었습니다. MusicXML을 별도로 선택하세요.")
    return value


def _file_specs(reader):
    """Name trees, catalog/page associated files, and file-attachment annotations."""
    catalog = reader.root_object
    names = _object(catalog.get("/Names"))
    tree = _object(names.get("/EmbeddedFiles")) if isinstance(names, DictionaryObject) else None
    stack, visited = ([tree] if tree is not None else []), set()
    while stack:
        node = _object(stack.pop())
        if not isinstance(node, DictionaryObject) or id(node) in visited:
            raise ValueError(
                "PDF 첨부파일 이름 목록이 손상되었습니다. MusicXML을 별도로 선택하세요."
            )
        visited.add(id(node))
        if len(visited) > 256:
            raise ValueError("PDF 첨부파일 이름 목록이 너무 큽니다.")
        if "/Names" in node:
            entries = _array(node["/Names"])
            if len(entries) % 2:
                raise ValueError("PDF 첨부파일 이름 목록이 손상되었습니다.")
            for index in range(0, len(entries), 2):
                yield str(_object(entries[index])), _object(entries[index + 1])
        if "/Kids" in node:
            stack.extend(_array(node["/Kids"]))

    def associated(owner):
        if "/AF" in owner:
            for item in _array(owner["/AF"]):
                yield "", _object(item)

    yield from associated(catalog)
    for page in reader.pages:
        yield from associated(page)
        for annotation in _array(page["/Annots"]) if "/Annots" in page else ():
            annotation = _object(annotation)
            if not isinstance(annotation, DictionaryObject):
                continue
            yield from associated(annotation)
            if annotation.get("/Subtype") == "/FileAttachment" and "/FS" in annotation:
                yield "", _object(annotation["/FS"])


def _attachment_bytes(stream, *, required):
    filters = _object(stream.get("/Filter", ArrayObject()))
    if filters is None:
        filters = ArrayObject()
    filters = filters if isinstance(filters, ArrayObject) else [filters]
    if any(_object(item) not in _TEXT_FILTERS for item in filters):
        if required:
            raise ValueError(
                "PDF의 MusicXML 첨부 압축 형식을 읽지 못했습니다. 원본을 별도로 선택하세요."
            )
        return None
    if len(stream._data) > MUSICXML_LIMIT:
        raise ValueError("PDF 첨부파일이 너무 큽니다 (최대 32 MB).")
    params = _object(stream.get("/Params"))
    if isinstance(params, DictionaryObject):
        if int(_object(params.get("/Size")) or 0) > MUSICXML_LIMIT:
            raise ValueError("PDF 첨부파일이 너무 큽니다 (최대 32 MB).")
    if not filters:
        return stream._data
    # The pinned pypdf exposes context-local limits for every expanding text filter.
    # Do not change a global decoder limit or call external image decoders.
    with apply_configuration(
        maximum_declared_stream_length=MUSICXML_LIMIT + 1,
        array_based_stream_maximum_output_length=MUSICXML_LIMIT + 1,
        zlib_maximum_output_length=MUSICXML_LIMIT + 1,
        lzw_maximum_output_length=MUSICXML_LIMIT + 1,
        run_length_maximum_output_length=MUSICXML_LIMIT + 1,
        brotli_maximum_output_length=MUSICXML_LIMIT + 1,
        stream_decoding_work_maximum_length=4 * MUSICXML_LIMIT,
    ):
        data = stream.get_data()
    if len(data) > MUSICXML_LIMIT:
        raise ValueError("PDF 첨부파일이 너무 큽니다 (최대 32 MB).")
    return data


def embedded_musicxml(reader, folder: Path) -> Path | None:
    """Stage one unique attached score; unrelated PDF metadata is never notation."""
    seen, documents, budget = set(), {}, 0
    for name, spec in _file_specs(reader):
        if not isinstance(spec, DictionaryObject):
            raise ValueError("PDF 첨부파일 정보가 손상되었습니다. MusicXML을 별도로 선택하세요.")
        if id(spec) in seen:
            continue
        seen.add(id(spec))
        if len(seen) > MAX_EMBEDDED_FILES:
            raise ValueError("PDF 첨부파일이 너무 많습니다. MusicXML을 별도로 선택하세요.")
        if _object(spec.get("/FS")) == "/URL":
            continue
        names = [name, str(_object(spec.get("/UF")) or ""), str(_object(spec.get("/F")) or "")]
        required = any(value.lower().endswith((".musicxml", ".mxl")) for value in names)
        files = _object(spec.get("/EF"))
        stream = None
        if isinstance(files, DictionaryObject):
            with apply_configuration(maximum_declared_stream_length=MUSICXML_LIMIT + 1):
                stream = _object(files.get("/UF"))
                if stream is None:
                    stream = _object(files.get("/F"))
        if not isinstance(stream, StreamObject):
            if required:
                raise ValueError("PDF의 MusicXML 첨부 내용이 없습니다. 원본을 별도로 선택하세요.")
            continue
        required = required or _object(stream.get("/Subtype")) in _SCORE_MIME_TYPES
        try:
            data = _attachment_bytes(stream, required=required)
        except (ValueError, TypeError, KeyError, NotImplementedError, PyPdfError) as error:
            raise ValueError(
                "PDF 첨부파일을 안전하게 읽지 못했습니다. MusicXML을 별도로 선택하세요."
            ) from error
        if data is None:
            continue
        budget += len(data)
        if budget > 2 * MUSICXML_LIMIT:
            raise ValueError("PDF 첨부파일 전체 크기가 너무 큽니다. MusicXML을 별도로 선택하세요.")
        score_header = re.search(
            rb"<(?:[A-Za-z_][\w.-]*:)?score-(?:partwise|timewise)\b", data[:8192]
        )
        required = required or bool(score_header)
        try:
            document = read_musicxml_bytes(data)
            root = ET.fromstring(document)
        except (ValueError, ET.ParseError, DefusedXmlException, zipfile.BadZipFile,
                KeyError, StopIteration, RuntimeError) as error:
            if required:
                raise ValueError(
                    "PDF의 MusicXML 첨부가 손상되었습니다. 올바른 MusicXML을 선택하세요."
                ) from error
            continue
        if root.tag.split("}")[-1] != "score-partwise":
            if required:
                raise ValueError(
                    "PDF 첨부는 score-partwise MusicXML이어야 합니다. 원본을 확인하세요."
                )
            continue
        if document is not data:
            budget += len(document)
            if budget > 2 * MUSICXML_LIMIT:
                raise ValueError("압축 해제한 PDF 첨부파일 전체 크기가 너무 큽니다.")
        documents[sha256(document).hexdigest()] = document
        if len(documents) > 1:
            raise ValueError(
                "PDF에 서로 다른 MusicXML 악보가 여러 개 있습니다. 사용할 원본을 별도로 선택하세요."
            )
    if not documents:
        return None
    digest, document = next(iter(documents.items()))
    destination = folder / "notation" / f"embedded-{digest}.musicxml"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and destination.stat().st_size == len(document):
        if sha256(destination.read_bytes()).hexdigest() == digest:
            return destination
    temporary = destination.with_name(f".embedded-{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as output:
            output.write(document)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination
