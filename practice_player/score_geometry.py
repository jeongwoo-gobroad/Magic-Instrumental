# SPDX-License-Identifier: GPL-3.0-only
"""Worker-only PDF geometry: independent of MusicXML print/width metadata."""

import ctypes
import math

import numpy as np
from scipy import ndimage

LAYOUT_VERSION = 2


def groups(indices, gap=1):
    values = np.asarray(indices)
    return [g for g in np.split(values, np.flatnonzero(np.diff(values) > gap) + 1) if len(g)]


def ink_mask(gray):
    background = float(np.percentile(gray, 90))
    return gray < max(60, min(195, background - 30))


def find_staves(ink):
    """Recover regular groups of five/six lines, including short final systems."""
    height, width = ink.shape
    # A separable min/max filter is linear in image size, independent of line length.
    closed = ndimage.maximum_filter1d(ink, 3, axis=1)
    closed = ndimage.minimum_filter1d(closed, 3, axis=1)
    length = max(15, round(width * 0.055))
    horizontal = ndimage.minimum_filter1d(closed, length, axis=1)
    horizontal = ndimage.maximum_filter1d(horizontal, length, axis=1)
    strength = horizontal.sum(axis=1)
    rows = groups(np.flatnonzero(strength > max(20, width * 0.18)))
    lines = [float(np.mean(g[strength[g] >= strength[g].max() * 0.9])) for g in rows]
    staves, cursor = [], 0
    while cursor + 5 <= len(lines):
        found = False
        for count in (6, 5):
            staff = lines[cursor : cursor + count]
            if len(staff) != count:
                continue
            gaps = np.diff(staff)
            spacing = float(np.median(gaps))
            if not 2 <= spacing <= height * 0.035:
                continue
            if max(abs(gaps - spacing)) > max(1.5, spacing * 0.22):
                continue
            common = horizontal[np.round(staff).astype(int)].sum(axis=0) >= count - 1
            spans = groups(np.flatnonzero(common))
            if not spans:
                continue
            span = max(spans, key=len)
            if len(span) < width * 0.12:
                continue
            staves.append(
                {
                    "lines": staff,
                    "spacing": spacing,
                    "left": float(span[0]),
                    "right": float(span[-1]),
                }
            )
            cursor += count
            found = True
            break
        if not found:
            cursor += 1
    if not staves:
        raise ValueError("PDF에서 일정 간격의 5선/6선 보표를 찾지 못했습니다.")
    return staves


def straighten(gray):
    """Estimate small scan skew only when the normal staff detector fails."""
    ink = ink_mask(gray)
    try:
        return ink, find_staves(ink), 0.0
    except ValueError:
        pass
    scale = min(1.0, 1000 / max(gray.shape))
    small = ndimage.zoom(ink, scale, order=0) if scale < 1 else ink

    def strength(angle):
        rotated = ndimage.rotate(small, angle, reshape=False, order=0, cval=0)
        rows = rotated.sum(axis=1).astype(float)
        return float(np.sum(rows * rows))

    coarse = np.arange(-3.0, 3.01, 0.5)
    best = max(coarse, key=strength)
    fine = np.arange(best - 0.4, best + 0.401, 0.1)
    angle = float(max(fine, key=strength))
    rotated = ndimage.rotate(gray, angle, reshape=False, order=1, cval=255)
    corrected = ink_mask(rotated)
    return corrected, find_staves(corrected), angle


def vector_verticals(page, bitmap):
    """Read stroked/filled PDF paths, composing nested form transformations."""
    import pypdfium2.raw as raw

    converter = bitmap.get_posconv(page)
    segments, budget = [], [0]

    def walk(form=None, matrices=(), depth=0):
        if depth > 8:
            return
        for obj in page.get_objects(max_depth=1, form=form):
            budget[0] += 1
            if budget[0] > 100000:
                raise ValueError("PDF 벡터 객체가 너무 많습니다.")
            chain = (*matrices, obj.get_matrix())
            if obj.type == raw.FPDF_PAGEOBJ_FORM:
                walk(obj, chain, depth + 1)
                continue
            if obj.type != raw.FPDF_PAGEOBJ_PATH:
                continue
            previous, start = None, None
            count = raw.FPDFPath_CountSegments(obj)
            if count > 100000:
                raise ValueError("PDF 경로가 너무 복잡합니다.")
            for index in range(count):
                segment = raw.FPDFPath_GetPathSegment(obj, index)
                xx, yy = ctypes.c_float(), ctypes.c_float()
                if not raw.FPDFPathSegment_GetPoint(segment, xx, yy):
                    continue
                x, y = xx.value, yy.value
                for matrix in reversed(chain):
                    x, y = (
                        matrix.a * x + matrix.c * y + matrix.e,
                        matrix.b * x + matrix.d * y + matrix.f,
                    )
                point = converter.to_bitmap(x, y)
                kind = raw.FPDFPathSegment_GetType(segment)
                if kind == raw.FPDF_SEGMENT_MOVETO:
                    start = point
                if kind == raw.FPDF_SEGMENT_LINETO and previous is not None:
                    segments.append((previous, point))
                if raw.FPDFPathSegment_GetClose(segment) and start is not None:
                    segments.append((point, start))
                previous = point

    walk()
    return [
        (float((a[0] + b[0]) / 2), float(min(a[1], b[1])), float(max(a[1], b[1])))
        for a, b in segments
        if abs(a[0] - b[0]) <= 1 and abs(a[1] - b[1]) >= 6
    ]


def barlines(ink, staff, vectors=(), system_bounds=None):
    spacing = staff["spacing"]
    top, bottom = round(staff["lines"][0]), round(staff["lines"][-1])
    left, right = round(staff["left"]), round(staff["right"])
    coverage = ink[top : bottom + 1, left : right + 1].mean(axis=0)
    candidates = []
    margin = max(2, round(spacing * 0.7))
    for group in groups(np.flatnonzero(coverage > 0.97)):
        x = left + int(round(float(np.mean(group))))
        above = ink[max(0, top - margin) : top, x].mean() if top > 0 else 0
        below = ink[bottom + 1 : bottom + 1 + margin, x].mean() if bottom + 1 < ink.shape[0] else 0
        # Stems extending out of the staff are not barlines. Edge braces are retained.
        if min(x - left, right - x) > spacing * 2 and max(above, below) > 0.65:
            continue
        # A note stem may span the whole staff when its notehead/beam fills the
        # remaining gap. Reject wide caps after removing the horizontal staff ink.
        capped = False
        for edge in (top, bottom):
            lo, hi = max(0, round(edge - spacing * 0.65)), round(edge + spacing * 0.65) + 1
            start = max(0, round(x - spacing))
            patch = ink[lo:hi, start : round(x + spacing) + 1].copy()
            line_rows = np.abs(np.arange(lo, hi) - edge) <= max(1.5, spacing * 0.17)
            removed = patch.copy()
            removed[line_rows] = False
            # Preserve only the candidate's actual line through the erased staff.
            gx0, gx1 = left + int(group[0]) - start, left + int(group[-1]) - start + 1
            removed[:, gx0:gx1] = patch[:, gx0:gx1]
            labels, _ = ndimage.label(removed, np.ones((3, 3)))
            label = labels[edge - lo, x - start]
            component = labels == label if label else np.zeros_like(removed)
            columns = np.flatnonzero(component.any(axis=0))
            if len(columns) and columns[-1] - columns[0] > max(4, spacing * 0.75):
                capped = True
        if capped:
            continue
        candidates.append(x)
    strong = []
    for x, y0, y1 in vectors:
        if not left - spacing <= x <= right + spacing:
            continue
        bounds = [(top, bottom)] + ([system_bounds] if system_bounds else [])
        tolerance = max(1.05, spacing * 0.10)
        column = round(x)
        visible = ink[top : bottom + 1, max(0, column - 1) : column + 2].any(axis=1).mean()
        if visible > 0.9 and any(
            abs(y0 - a) <= tolerance and abs(y1 - b) <= tolerance for a, b in bounds
        ):
            strong.append(x)
    if strong:
        # Original path endpoints distinguish full-height barlines from glyph stems.
        candidates = sorted(strong)
    # Double/final/repeat bars describe one boundary, not extra tiny measures.
    boundaries = [float(np.mean(g)) for g in groups(candidates, gap=spacing * 1.6)]
    if not boundaries or boundaries[0] > left + spacing * 2:
        boundaries.insert(0, float(left))
    else:
        boundaries[0] = float(left)
    if boundaries[-1] < right - spacing * 2:
        boundaries.append(float(right))
    else:
        boundaries[-1] = float(right)
    if (
        len(boundaries) > 2
        and boundaries[1] - left < spacing * 12
        and repeat_dots(ink, staff, boundaries[1], 1)
    ):
        # A repeat start after clef/key/time is a system header, not a pickup bar.
        boundaries.pop(0)
    return boundaries


def original_point(x, y, angle, shape):
    height, width = shape
    cx, cy = (width - 1) / 2, (height - 1) / 2
    radians = math.radians(angle)
    return (
        math.cos(radians) * (x - cx) - math.sin(radians) * (y - cy) + cx,
        math.sin(radians) * (x - cx) + math.cos(radians) * (y - cy) + cy,
    )


def normalized_box(left, top, right, bottom, angle, shape):
    points = [original_point(x, y, angle, shape) for x in (left, right) for y in (top, bottom)]
    height, width = shape
    return {
        "left": max(0.0, min(x for x, _ in points) / width),
        "right": min(1.0, max(x for x, _ in points) / width),
        "top": max(0.0, min(y for _, y in points) / height),
        "bottom": min(1.0, max(y for _, y in points) / height),
    }


def repeat_dots(ink, staff, boundary, side):
    """Require two isolated round dots at repeat-sign staff positions."""
    if len(staff["lines"]) != 5:
        return False
    d = staff["spacing"]
    y0 = staff["lines"][0]
    x0, x1 = sorted((boundary + side * d * 0.55, boundary + side * d * 2.6))
    x0, x1 = max(0, int(x0)), min(ink.shape[1], int(x1) + 1)
    centers = []
    for target in (y0 + d * 1.5, y0 + d * 2.5):
        top, bottom = max(0, int(target - d * 0.38)), int(target + d * 0.38) + 1
        labels, _ = ndimage.label(ink[top:bottom, x0:x1])
        found = []
        for box in ndimage.find_objects(labels):
            if box is None:
                continue
            ys, xs = box
            h, w = ys.stop - ys.start, xs.stop - xs.start
            if not (d * 0.12 <= h <= d * 0.65 and d * 0.12 <= w <= d * 0.65):
                continue
            if not 0.55 <= w / h <= 1.8:
                continue
            # Cropped components can be stems/notes, not isolated dots.
            if ys.start == 0 or ys.stop == bottom - top:
                continue
            found.append(x0 + (xs.start + xs.stop) / 2)
        centers.append(found)
    return any(abs(a - b) <= d * 0.35 for a in centers[0] for b in centers[1])


def page_geometry(gray, pattern, vectors=()):
    ink, staves, angle = straighten(gray)
    if len(staves) % len(pattern):
        raise ValueError("PDF의 보표 수와 MusicXML 파트/보표 구성이 다릅니다.")
    systems = []
    for first in range(0, len(staves), len(pattern)):
        group = staves[first : first + len(pattern)]
        if [len(s["lines"]) for s in group] != pattern:
            raise ValueError("PDF의 5선/6선 보표와 MusicXML 보표 구성이 다릅니다.")
        bounds = (group[0]["lines"][0], group[-1]["lines"][-1])
        boundaries = [barlines(ink, s, vectors if angle == 0 else (), bounds) for s in group]
        # Multiple simultaneous staves must agree, not add their measures together.
        if any(len(b) != len(boundaries[0]) for b in boundaries):
            raise ValueError("같은 연주 줄의 보표별 마디선 수가 달라 확인이 필요합니다.")
        d = float(np.median([s["spacing"] for s in group]))
        xs = np.median(boundaries, axis=0)
        if any(np.max(abs(np.asarray(b) - xs)) > d * 1.6 for b in boundaries):
            raise ValueError("같은 연주 줄의 보표별 마디선 위치가 일치하지 않습니다.")
        if any(np.diff(xs) < d * 2):
            raise ValueError("지나치게 좁은 마디 후보가 있습니다. 마디선을 확인하세요.")
        top = group[0]["lines"][0] - 2.5 * d
        bottom = group[-1]["lines"][-1] + 3.5 * d
        box = normalized_box(xs[0], top, xs[-1], bottom, angle, ink.shape)
        bars = []
        for i in range(len(xs) - 1):
            bar = normalized_box(xs[i], top, xs[i + 1], bottom, angle, ink.shape)
            bar.update(
                fraction0=float((xs[i] - xs[0]) / (xs[-1] - xs[0])),
                fraction1=float((xs[i + 1] - xs[0]) / (xs[-1] - xs[0])),
                repeat_start=repeat_dots(ink, group[0], xs[i], 1),
                repeat_end=repeat_dots(ink, group[0], xs[i + 1], -1),
            )
            bars.append(bar)
        systems.append({"box": box, "bars": bars})
    return systems, angle


def xml_staff_pattern(xml):
    from defusedxml import ElementTree as ET

    from .score_analysis import read_musicxml

    root = ET.fromstring(read_musicxml(xml))
    for node in root.iter():
        node.tag = node.tag.split("}")[-1]
    parts = root.findall("part")
    if root.tag != "score-partwise" or not parts:
        raise ValueError("MusicXML 파트를 찾지 못했습니다.")
    count = len(parts[0].findall("measure"))
    if not 1 <= count <= 2000:
        raise ValueError("자동 배치는 1~2,000마디를 지원합니다.")
    pattern = []
    for part in parts:
        if len(part.findall("measure")) != count:
            raise ValueError("파트별 마디 수가 달라 배치를 자동 연결할 수 없습니다.")
        first = part.find("measure")
        n = int(first.findtext("attributes/staves", "1"))
        if not 1 <= n <= 32:
            raise ValueError("MusicXML 보표 수가 범위를 벗어났습니다.")
        lines = {
            int(x.get("number", "1")): int(x.findtext("staff-lines", "5"))
            for x in first.findall("attributes/staff-details")
        }
        for measure in part.findall("measure")[1:]:
            for attrs in measure.findall("attributes"):
                if int(attrs.findtext("staves", str(n))) != n:
                    raise ValueError("중간에 보표 수가 바뀝니다. MusicXML 인쇄 배치가 필요합니다.")
                for details in attrs.findall("staff-details"):
                    number = int(details.get("number", "1"))
                    if int(details.findtext("staff-lines", str(lines.get(number, 5)))) != lines.get(
                        number, 5
                    ):
                        raise ValueError("중간에 보표 선 수가 바뀝니다. 인쇄 배치를 확인하세요.")
        pattern.extend(lines.get(i, 5) for i in range(1, n + 1))
    if not count or count > 2000 or len(pattern) > 32 or any(n not in (5, 6) for n in pattern):
        raise ValueError("자동 배치는 5선/6선 보표와 최대 2,000마디를 지원합니다.")
    return pattern, count


def paper_format(size):
    """Identify common paper sizes from the visible PDF page, in either orientation."""
    short, long = sorted(size)
    for name, width, height in (
        ("A4", 595.28, 841.89),
        ("A3", 841.89, 1190.55),
        ("A5", 419.53, 595.28),
        ("Letter", 612, 792),
        ("Legal", 612, 1008),
    ):
        if abs(short - width) <= 3 and abs(long - height) <= 3:
            return name
    return "custom"


def detect_pdf_layout(pdf, xml, report=lambda *_: None):
    import pypdfium2

    pattern, expected = xml_staff_pattern(xml)
    sizes, bars, pages = [], {}, []
    with pypdfium2.PdfDocument(str(pdf)) as document:
        if not 1 <= len(document) <= 200:
            raise ValueError("PDF 페이지 수가 범위를 벗어났습니다.")
        for number in range(len(document)):
            report(f"PDF 자체 배치 분석 · {number + 1}/{len(document)}페이지 보표·마디선", None)
            page = document[number]
            try:
                size = list(page.get_size())
                bitmap = page.render(scale=min(3, 3200 / max(size)))
                try:
                    gray = np.asarray(bitmap.to_pil().convert("L"))
                    systems, angle = page_geometry(gray, pattern, vector_verticals(page, bitmap))
                finally:
                    bitmap.close()
                sizes.append(size)
                pages.append(
                    {
                        "size_points": size,
                        "format": paper_format(size),
                        "orientation": "landscape" if size[0] > size[1] else "portrait",
                        "deskew_degrees": round(angle, 3),
                        "systems": len(systems),
                    }
                )
                for system_index, system in enumerate(systems):
                    following = systems[min(system_index + 1, len(systems) - 1)]["box"]["top"]
                    for bar in system["bars"]:
                        bars[len(bars)] = {
                            **bar,
                            "page": number,
                            "system": system_index,
                            "next_top": following,
                        }
            finally:
                page.close()
    if len(bars) != expected:
        raise ValueError(
            f"PDF 마디선 {len(bars)}마디와 MusicXML {expected}마디가 일치하지 않습니다. "
            "복합 쉼표·숨긴 파트·인식 결과를 확인하세요."
        )
    return (
        sizes,
        bars,
        {
            "version": LAYOUT_VERSION,
            "method": "pdf-staves-barlines",
            "pages": pages,
            "measure_count": len(bars),
        },
    )
