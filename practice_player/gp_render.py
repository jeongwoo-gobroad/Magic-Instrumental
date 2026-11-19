# SPDX-License-Identifier: GPL-3.0-only
"""Worker-only GP engraving; PDF is generated from notes, never recognized again."""

import json
import os
import uuid
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from xml.etree import ElementTree as XML

from defusedxml import ElementTree as SafeXML
from filelock import FileLock

from .cache import hash_file
from .guitar_pro import GP_VERSION, list_tracks, node, read_gp
from .score import atomic_json

RENDER_VERSION = 2
VEROVIO_VERSION = "6.4.1"
SVG = "{http://www.w3.org/2000/svg}"


def display_xml(data, metadata):
    """Add duplicate guitar TAB for display without duplicating alignment notes."""
    root = SafeXML.fromstring(data)
    previous_time = None
    for i, measure in enumerate(root.findall("part/measure")):
        attrs = measure.find("attributes")
        # Verovio needs a visual duration for partial padding rests. The original
        # conversion keeps exact duration ticks for alignment; enrich display only.
        for note in measure.findall("note"):
            if note.find("rest") is not None and note.find("type") is None:
                from fractions import Fraction

                duration = Fraction(
                    int(note.findtext("duration")), int(attrs.findtext("divisions"))
                )
                if note.find("rest").get("measure") == "yes":
                    continue
                names = {
                    4: "whole",
                    2: "half",
                    1: "quarter",
                    Fraction(1, 2): "eighth",
                    Fraction(1, 4): "16th",
                    Fraction(1, 8): "32nd",
                }
                shape = names.get(duration)
                node(note, "type", shape or "quarter")
                if shape is None:
                    modification = node(note, "time-modification")
                    node(modification, "actual-notes", duration.denominator)
                    node(modification, "normal-notes", duration.numerator)
        meter = attrs.find("time")
        signature = (meter.findtext("beats"), meter.findtext("beat-type"))
        length = int(attrs.findtext("divisions")) * int(signature[0]) * 4 // int(signature[1])
        if signature == previous_time:
            attrs.remove(meter)
        previous_time = signature
        if metadata["percussion"]:
            continue
        node(attrs, "staves", 2)
        if i == 0:
            attrs.find("clef").set("number", "1")
            clef = node(attrs, "clef", number="2")
            node(clef, "sign", "TAB")
            node(clef, "line", 5)
            details = node(attrs, "staff-details", number="2")
            node(details, "staff-lines", len(metadata["strings"]))
            names = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
            for string in metadata["strings"]:
                tuning = node(
                    details,
                    "staff-tuning",
                    line=str(len(metadata["strings"]) + 1 - string["number"]),
                )
                name = names[string["midi"] % 12]
                node(tuning, "tuning-step", name[0])
                if len(name) == 2:
                    node(tuning, "tuning-alter", 1)
                node(tuning, "tuning-octave", string["midi"] // 12 - 1)
        copies = [deepcopy(n) for n in measure if n.tag in {"note", "backup", "forward"}]
        for note in measure.findall("note"):
            node(note, "staff", 1)
        node(node(measure, "backup"), "duration", length)
        for child in copies:
            if child.tag == "note":
                node(child, "staff", 2)
            measure.append(child)
    return XML.tostring(root, encoding="unicode")


def flatten_svg(svg):
    """Flatten Verovio's nested viewport and debug boxes for Qt SVG Tiny."""
    root = SafeXML.fromstring(svg)
    inner = root.find(SVG + "svg")
    if inner is None:
        raise ValueError("GP 악보 화면의 좌표를 읽지 못했습니다.")
    width, height = map(float, inner.get("viewBox").split()[2:])
    root.set("viewBox", inner.attrib.pop("viewBox"))
    inner.tag = SVG + "g"
    margin = inner.find(SVG + "g")
    if margin.get("transform") != "translate(500, 500)":
        raise ValueError("GP 렌더러의 페이지 여백이 변경되었습니다.")
    bars = {}
    for system_index, system in enumerate(root.iter(SVG + "g")):
        if system.get("class") != "system":
            continue
        row = []
        for measure in system.iter(SVG + "g"):
            if measure.get("class") != "measure":
                continue
            identity = measure.get("id", "")
            if not identity.startswith("gp-bar-"):
                raise ValueError("GP 마디 식별자가 누락되었습니다.")
            rects = []
            for staff in measure.iter(SVG + "g"):
                if staff.get("class") == "staff bounding-box":
                    rect = staff.find(SVG + "rect")
                    if rect is not None:
                        rects.append({k: float(rect.get(k)) for k in ("x", "y", "width", "height")})
            if not rects:
                raise ValueError("GP 마디의 표시 영역이 비어 있습니다.")
            index = int(identity[7:]) - 1
            box = {
                "left": max(0, min(r["x"] for r in rects) + 500) / width,
                "right": min(width, max(r["x"] + r["width"] for r in rects) + 500) / width,
                "top": max(0, min(r["y"] for r in rects) + 200) / height,
                "bottom": min(height, max(r["y"] + r["height"] for r in rects) + 800) / height,
                "system": system_index,
            }
            bars[index] = box
            row.append(box)
        if row:
            left, right = min(b["left"] for b in row), max(b["right"] for b in row)
            for box in row:
                box.update(
                    fraction0=(box["left"] - left) / (right - left),
                    fraction1=(box["right"] - left) / (right - left),
                )
    for parent in root.iter():
        for child in list(parent):
            classes = child.get("class", "").split()
            if "bounding-box" in classes or "placeholder" in classes:
                parent.remove(child)
    # SVG debug boxes nested in tspans and a zero-size text root are not Qt Tiny.
    for text in root.iter(SVG + "text"):
        sizes = [c.get("font-size") for c in text.iter() if c.get("font-size") not in (None, "0px")]
        value = "".join(
            c.text or "" for c in text.iter() if c.tag in {SVG + "text", SVG + "tspan"}
        ).strip()
        for child in list(text):
            text.remove(child)
        text.text = value
        text.set("font-size", sizes[0] if sizes else "324px")
    XML.register_namespace("", SVG[1:-1])
    XML.register_namespace("xlink", "http://www.w3.org/1999/xlink")
    return XML.tostring(root), bars


def prepare_gp(source, paths, track=1, report=lambda *_: None):
    """Return verified display assets and track names; no audio alignment yet."""
    source = Path(source).resolve(strict=True)
    digest = hash_file(source)
    settings = {
        "source": digest,
        "track": track,
        "render": RENDER_VERSION,
        "gp": GP_VERSION,
        "verovio": VEROVIO_VERSION,
    }
    key = sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()[:20]
    folder = paths.processed / "gp-scores" / digest / key
    folder.mkdir(parents=True, exist_ok=True)
    with FileLock(str(folder / ".render.lock"), timeout=120):
        manifest = folder / "display.json"
        if manifest.is_file():
            info = json.loads(manifest.read_text(encoding="utf-8"))
            if info.get("signature") == settings and all(
                (folder / name).is_file() and hash_file(folder / name) == value
                for name, value in info["files"].items()
            ):
                return {
                    **info,
                    "pdf": str(folder / "score.pdf"),
                    "layout": str(manifest),
                    "source": str(source),
                    "cached": True,
                }
        report("GP 파트를 악보로 준비하고 있습니다…", None)
        data, metadata = read_gp(source, track)
        import verovio

        renderer = verovio.toolkit()
        renderer.setOptions(
            {
                "pageWidth": 2100,
                "pageHeight": 2970,
                "scale": 40,
                "svgViewBox": True,
                "svgBoundingBoxes": True,
                "xmlIdSeed": 1,
                "mnumInterval": 1,
                "pageMarginLeft": 50,
                "pageMarginRight": 50,
                "pageMarginTop": 50,
                "pageMarginBottom": 50,
            }
        )
        if not renderer.loadData(display_xml(data, metadata)):
            raise ValueError("GP 악보를 그리지 못했습니다. 다른 파트를 선택해 보세요.")
        count = renderer.getPageCount()
        if not 1 <= count <= 200:
            raise ValueError("GP 악보는 최대 200페이지까지 표시할 수 있습니다.")
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtCore import QMarginsF, QRectF
        from PySide6.QtGui import QPageLayout, QPageSize, QPainter, QPdfWriter
        from PySide6.QtSvg import QSvgRenderer
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication([])
        temporary = folder / f".{uuid.uuid4().hex}.pdf"
        writer = QPdfWriter(str(temporary))
        writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
        writer.setPageMargins(QMarginsF(0, 0, 0, 0), QPageLayout.Unit.Millimeter)
        writer.setResolution(144)
        writer.setCreator("Magic-Instrumental")
        writer.setTitle(metadata["track_name"])
        painter = QPainter(writer)
        bars, sizes = {}, []
        try:
            for number in range(1, count + 1):
                report(f"{metadata['track_name']} · {number}/{count}페이지 준비", number / count)
                svg, page_bars = flatten_svg(renderer.renderToSVG(number))
                if number > 1:
                    writer.newPage()
                graphic = QSvgRenderer(svg)
                if not graphic.isValid():
                    raise ValueError("GP 악보 화면이 올바르지 않습니다.")
                graphic.render(painter, QRectF(0, 0, writer.width(), writer.height()))
                (folder / f"page-{number}.svg").write_bytes(svg)
                for i, box in page_bars.items():
                    if i in bars:
                        raise ValueError("GP 마디가 중복 배치되었습니다.")
                    bars[i] = {**box, "page": number - 1}
                sizes.append([595.0, 842.0])
            if set(bars) != set(range(metadata["measures"])):
                raise ValueError("GP 원본 마디와 화면의 마디가 일치하지 않습니다.")
        finally:
            painter.end()
            del writer
            app.processEvents()
        temporary.replace(folder / "score.pdf")
        for i, box in bars.items():
            later = next(
                (
                    b
                    for n, b in sorted(bars.items())
                    if n > i and b["page"] == box["page"] and b["system"] != box["system"]
                ),
                box,
            )
            box["next_top"] = later["top"]
        (folder / "score.musicxml").write_bytes(data)
        if hash_file(source) != digest:
            raise ValueError("준비 중 GP 원본이 바뀌었습니다. 다시 열어 주세요.")
        files = {
            p.name: hash_file(p)
            for p in folder.iterdir()
            if p.suffix in {".svg", ".pdf", ".musicxml"} and not p.name.startswith(".")
        }
        info = {
            "signature": settings,
            "metadata": metadata,
            "tracks": list_tracks(source),
            "bars": bars,
            "page_sizes": sizes,
            "files": files,
        }
        atomic_json(manifest, info)
        return {
            **info,
            "pdf": str(folder / "score.pdf"),
            "layout": str(manifest),
            "source": str(source),
            "cached": False,
        }


def native_layout(info):
    """Verify generated geometry before analysis/export; never called during playback."""
    manifest = Path(info["layout"])
    saved = json.loads(manifest.read_text(encoding="utf-8"))
    if (
        saved["signature"] != info["signature"]
        or hash_file(Path(info["pdf"])) != saved["files"]["score.pdf"]
    ):
        raise ValueError("GP 표시 파일이 변경되었습니다. 악보를 다시 열어 주세요.")
    bars = {int(k): v for k, v in saved["bars"].items()}
    return (
        saved["page_sizes"],
        bars,
        {"method": "guitar-pro-native", "measure_count": len(bars), "warnings": []},
    )


def verify_runtime():
    import guitarpro
    import verovio

    assert callable(guitarpro.parse)
    renderer = verovio.toolkit()
    score = (
        '<score-partwise version="4.0"><part-list><score-part id="P1">'
        '<part-name>Guitar</part-name></score-part></part-list><part id="P1">'
        '<measure number="1"><attributes><divisions>1</divisions>'
        "<time><beats>4</beats><beat-type>4</beat-type></time>"
        "<clef><sign>G</sign><line>2</line></clef></attributes>"
        '<note><rest measure="yes"/><duration>4</duration></note>'
        "</measure></part></score-partwise>"
    )
    if not renderer.loadData(score) or renderer.getPageCount() != 1:
        raise RuntimeError("GP 악보 표시 도구 검증에 실패했습니다.")
    if "<svg" not in renderer.renderToSVG(1):
        raise RuntimeError("GP 악보 화면을 만들지 못했습니다.")
    print("GP 악보 도구 준비 완료")
