# SPDX-License-Identifier: GPL-3.0-only
"""Worker-only GP engraving; PDF is generated from notes, never recognized again."""

import json
import os
import sys
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

RENDER_VERSION = 4
VEROVIO_VERSION = "6.4.1"
SVG = "{http://www.w3.org/2000/svg}"
_render_app = None


def render_application():
    """Create an invisible worker app with a real platform font database."""
    from PySide6.QtWidgets import QApplication

    global _render_app
    app = QApplication.instance()
    if app is None:
        if sys.platform == "win32":
            # Qt's Windows offscreen plugin can supply only box glyphs. Creating
            # a native QApplication does not show a window; no QWidget is made.
            os.environ["QT_QPA_PLATFORM"] = "windows"
        else:
            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        _render_app = app = QApplication([])
    if sys.platform == "win32" and app.platformName() != "windows":
        raise ValueError("Windows 악보 글꼴을 사용할 수 없습니다. 앱을 다시 실행해 주세요.")
    return app


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


def outline_svg_text(root):
    """Convert labels/digits to vector paths before Qt SVG/PDF font embedding.

    Verovio's musical glyphs already use paths. Windows PDF font substitution
    must not decide how fret numbers or measure labels appear in saved assets.
    """
    from PySide6.QtGui import QFont, QFontMetricsF, QPainterPath

    serial = 0

    def visit(element, inherited):
        nonlocal serial
        styles = dict(inherited)
        classes = set(element.get("class", "").split())
        if classes & {"mNum", "dir", "dynam"}:
            styles["font-style"] = "italic"
        if classes & {"ending", "fing", "reh", "tempo"}:
            styles["font-weight"] = "bold"
        for name in ("font-style", "font-weight", "font-size"):
            if name in element.attrib:
                styles[name] = element.get(name)
        if element.tag != SVG + "text":
            for child in element:
                visit(child, styles)
            return
        spans = [e for e in element.iter() if e.tag in {SVG + "text", SVG + "tspan"}]
        value = "".join(e.text or "" for e in spans).strip()
        sizes = [e.get("font-size") for e in spans if e.get("font-size") not in (None, "0px")]
        size = float((sizes[0] if sizes else "324px").removesuffix("px"))
        if not 0 < size <= 4096:
            raise ValueError("GP 악보 글자 크기가 표시 범위를 벗어났습니다.")
        font = QFont()
        font.setFamilies(["Times New Roman", "Times", "Liberation Serif", "DejaVu Serif"])
        font.setStyleHint(QFont.StyleHint.Serif)
        font.setPixelSize(round(size))
        font.setItalic(styles.get("font-style") == "italic")
        font.setBold(styles.get("font-weight") == "bold")

        def attribute(name, default):
            return next((e.get(name) for e in spans if name in e.attrib), default)

        x, y = float(attribute("x", "0")), float(attribute("y", "0"))
        anchor = attribute("text-anchor", "start")
        advance = QFontMetricsF(font).horizontalAdvance(value)
        x -= advance * ({"middle": 0.5, "end": 1}.get(anchor, 0))
        outline = QPainterPath()
        outline.addText(x, y, font, value)
        commands, i = [], 0
        while i < outline.elementCount():
            point = outline.elementAt(i)
            if point.isMoveTo():
                commands.append(f"M{point.x:.4f},{point.y:.4f}")
            elif point.isLineTo():
                commands.append(f"L{point.x:.4f},{point.y:.4f}")
            else:
                second, end = outline.elementAt(i + 1), outline.elementAt(i + 2)
                commands.append(
                    f"C{point.x:.4f},{point.y:.4f} {second.x:.4f},{second.y:.4f} "
                    f"{end.x:.4f},{end.y:.4f}"
                )
                i += 2
            i += 1
        transform = element.get("transform")
        serial += 1
        identity = element.get("id", f"gp-text-{serial}")
        element.clear()
        element.tag = SVG + "g"
        element.set("class", "outlined-text")
        element.set("aria-label", value)
        if transform:
            element.set("transform", transform)
        if identity:
            element.set("id", identity)
        if commands:
            XML.SubElement(element, SVG + "path", {
                "d": " ".join(commands),
                # Inline style overrides Verovio's generic path stroke rule.
                "style": "fill:currentColor;stroke:none;stroke-width:0;fill-rule:nonzero",
            })

    visit(root, {})


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
    outline_svg_text(root)
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
        from PySide6.QtCore import QMarginsF, QRectF
        from PySide6.QtGui import QPageLayout, QPageSize, QPainter, QPdfWriter
        from PySide6.QtSvg import QSvgRenderer

        app = render_application()
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
