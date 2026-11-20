# SPDX-License-Identifier: GPL-3.0-only
"""Worker-only PDF geometry: independent of MusicXML print/width metadata."""

import ctypes
import math

import numpy as np
from scipy import ndimage

LAYOUT_VERSION = 4


def groups(indices, gap=1):
    values = np.asarray(indices)
    return [g for g in np.split(values, np.flatnonzero(np.diff(values) > gap) + 1) if len(g)]


def ink_mask(gray):
    background = float(np.percentile(gray, 90))
    return gray < max(60, min(195, background - 30))


def find_staves(ink, *, allow_four=False, repair=True):
    """Recover regular staff lines without letting gap repair replace clear staves."""
    height, width = ink.shape
    max_spacing = max(height * 0.035, width * 0.02) if allow_four else height * 0.035
    # A separable min/max filter is linear in image size, independent of line length.
    length = max(15, round(width * 0.055))

    def candidates(gap):
        closed = ndimage.maximum_filter1d(ink, gap, axis=1)
        closed = ndimage.minimum_filter1d(closed, gap, axis=1)
        horizontal = ndimage.minimum_filter1d(closed, length, axis=1)
        horizontal = ndimage.maximum_filter1d(horizontal, length, axis=1)
        strength = horizontal.sum(axis=1)
        rows = groups(np.flatnonzero(strength > max(20, width * 0.18)))
        return [float(np.mean(g[strength[g] >= strength[g].max() * 0.9])) for g in rows]

    lines = candidates(3)
    gaps = np.diff(lines)
    regular = gaps[(gaps >= 2) & (gaps <= max_spacing)]
    if repair and len(regular):
        # Fill missing densely numbered TAB lines without moving an already
        # clear staff line toward nearby notes or beams.
        tolerance = max(2, float(np.median(regular)) * 0.4)
        additions = [y for y in candidates(max(3, round(width * 0.014)))
                     if min(abs(y - x) for x in lines) > tolerance]
        try:
            stable = find_staves(ink, allow_four=allow_four, repair=False)
        except ValueError:
            stable = []
        safe = []
        for y in additions:
            if any(s["lines"][0] - tolerance <= y <= s["lines"][-1] + tolerance for s in stable):
                continue
            adjacent = [s for s in stable if min(abs(y - s["lines"][0]),
                                                abs(y - s["lines"][-1])) <= s["spacing"] * 1.5]
            if adjacent:
                staff = min(adjacent, key=lambda s: abs(y - np.mean(s["lines"])))
                edge = max(3, round(staff["spacing"] * 1.5))
                row = ink[max(0, round(y) - 1):round(y) + 2].any(axis=0)
                left, right = round(staff["left"]), round(staff["right"])
                # A real missing TAB line reaches both edges; cymbal rows do not.
                if min(row[left + 2:left + edge].mean(), row[right - edge:right - 2].mean()) < 0.65:
                    continue
            safe.append(y)
        lines = sorted(lines + safe)
    staves, cursor = [], 0
    minimum = 4 if allow_four else 5
    while cursor + minimum <= len(lines):
        found = False
        for count in ((6, 5, 4) if allow_four else (6, 5)):
            staff = lines[cursor : cursor + count]
            if len(staff) != count:
                continue
            gaps = np.diff(staff)
            spacing = float(np.median(gaps))
            if not 2 <= spacing <= max_spacing:
                continue
            if max(abs(gaps - spacing)) > max(1.5, spacing * 0.22):
                continue
            # Recover short gaps made by fret-number backgrounds before finding
            # the extent. Opening long lines alone drops densely numbered TAB.
            strips = np.array([
                ink[max(0, round(y) - 1):round(y) + 2].any(axis=0) for y in staff
            ])
            gap = max(3, round(spacing * 2))
            strips = ndimage.maximum_filter1d(strips, gap, axis=1)
            strips = ndimage.minimum_filter1d(strips, gap, axis=1)
            common = strips.sum(axis=0) >= count - 1
            spans = groups(np.flatnonzero(common))
            if not spans:
                continue
            span = max(spans, key=len)
            if len(span) < width * 0.12:
                continue
            if count == 6:
                # Long ledger runs below a five-line staff are not a sixth line.
                # Real TAB lines reach the staff edges even when digits obscure
                # their interior (morphological opening can remove those edges).
                edge_width = max(3, round(spacing * 1.5))
                y = round(staff[-1])
                row = ink[max(0, y - 1):y + 2].any(axis=0)
                if any(row[a:b].mean() < 0.65 for a, b in (
                    (span[0] + 2, span[0] + edge_width),
                    (span[-1] - edge_width, span[-1] - 2),
                )):
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


def straighten(gray, *, allow_four=False):
    """Estimate small scan skew only when the normal staff detector fails."""
    ink = ink_mask(gray)
    try:
        staves = find_staves(ink, allow_four=allow_four)
        # A skewed five-line staff can leave a short, apparently regular four-line fragment.
        fragmented = allow_four and any(
            len(s["lines"]) == 4 and s["right"] - s["left"] < ink.shape[1] * 0.45
            for s in staves
        )
        if not fragmented:
            return ink, staves, 0.0
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
    return corrected, find_staves(corrected, allow_four=allow_four), angle


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


def barlines(ink, staff, vectors=(), system_bounds=None, *, connected_bounds=(),
             prefer_vectors=True, reject_vector_caps=False):
    spacing = staff["spacing"]
    top, bottom = round(staff["lines"][0]), round(staff["lines"][-1])
    left, right = round(staff["left"]), round(staff["right"])
    coverage = ink[top : bottom + 1, left : right + 1].mean(axis=0)
    candidates = []
    margin = max(2, round(spacing * 0.7))
    def wide_caps(x, first, last):
        capped = False
        for edge in (top, bottom):
            lo, hi = max(0, round(edge - spacing * 0.65)), round(edge + spacing * 0.65) + 1
            start = max(0, round(x - spacing))
            patch = ink[lo:hi, start : round(x + spacing) + 1].copy()
            line_rows = np.abs(np.arange(lo, hi) - edge) <= max(1.5, spacing * 0.17)
            removed = patch.copy()
            removed[line_rows] = False
            # Preserve only the candidate's actual line through the erased staff.
            gx0, gx1 = max(0, first - start), min(patch.shape[1], last - start + 1)
            removed[:, gx0:gx1] = patch[:, gx0:gx1]
            labels, _ = ndimage.label(removed, np.ones((3, 3)))
            label = labels[edge - lo, x - start]
            component = labels == label if label else np.zeros_like(removed)
            columns = np.flatnonzero(component.any(axis=0))
            if len(columns) and columns[-1] - columns[0] > max(4, spacing * 0.75):
                wide_rows = np.count_nonzero(component.sum(axis=1) > max(4, spacing * 0.75))
                # Thin ties can cross genuine barlines. A notehead/beam fills several rows.
                center = x - start
                sides = [int(component[:, :max(0, center - 2)].sum()),
                         int(component[:, center + 3:].sum())]
                one_sided = min(sides) < max(sides) * 0.35
                if not reject_vector_caps or (
                    one_sided and wide_rows >= max(2, math.ceil(spacing * 0.2))
                ):
                    capped = True
        return capped

    for group in groups(np.flatnonzero(coverage > 0.97)):
        x = left + int(round(float(np.mean(group))))
        above = ink[max(0, top - margin) : top, x].mean() if top > 0 else 0
        below = ink[bottom + 1 : bottom + 1 + margin, x].mean() if bottom + 1 < ink.shape[0] else 0
        # Stems extending out of the staff are not barlines. Edge braces are retained.
        if min(x - left, right - x) > spacing * 2 and max(above, below) > 0.65:
            continue
        if wide_caps(x, left + int(group[0]), left + int(group[-1])):
            continue
        candidates.append(x)
    strong = []
    for x, y0, y1 in vectors:
        if not left - spacing <= x <= right + spacing:
            continue
        bounds = ([(top, bottom)] + ([system_bounds] if system_bounds else [])
                  + list(connected_bounds))
        tolerance = max(1.05, spacing * 0.10)
        column = round(x)
        visible = ink[top : bottom + 1, max(0, column - 1) : column + 2].any(axis=1).mean()
        if visible > 0.9 and any(
            abs(y0 - a) <= tolerance and abs(y1 - b) <= tolerance for a, b in bounds
        ):
            if reject_vector_caps and min(x - left, right - x) > spacing * 2:
                if wide_caps(column, column - 1, column + 1):
                    continue
            strong.append(x)
    if strong:
        # Original path endpoints distinguish full-height barlines from glyph stems.
        candidates = sorted(strong if prefer_vectors else set(candidates + strong))
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
    counts = [len(s["lines"]) for s in staves]
    # A standard-notation source may omit the duplicated TAB staff. Require the
    # full alternating pattern and matching barlines below before pairing it.
    companion = bool(pattern == [5] and counts == [5, 6] * (len(counts) // 2))
    if companion:
        pattern = [5, 6]
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
            if angle == 0:
                bar["staff_top"] = group[0]["lines"][0] / ink.shape[0]
                bar["staff_spacing"] = group[0]["spacing"] / ink.shape[0]
                bar["staff_spacing_x"] = group[0]["spacing"] / ink.shape[1]
            bars.append(bar)
        systems.append({"box": box, "bars": bars, "tab_companion": companion})
    return systems, angle


def raster_verticals(ink, spacing):
    """Keep continuous thin vertical strokes, including scanned system connectors."""
    length = max(8, round(spacing * 2.8))
    vertical = ndimage.minimum_filter1d(ink, length, axis=0)
    vertical = ndimage.maximum_filter1d(vertical, length, axis=0)
    labels, _ = ndimage.label(vertical)
    segments = []
    for box in ndimage.find_objects(labels):
        if box is None:
            continue
        ys, xs = box
        if ys.stop - ys.start >= length and xs.stop - xs.start <= max(5, spacing * 0.6):
            segments.append(((xs.start + xs.stop - 1) / 2, float(ys.start), float(ys.stop - 1)))
    return segments


def group_pdf_staves(ink, staves, vectors, known_patterns=()):
    """Join observed system connectors; leave clearly separated solo systems apart."""
    if len(staves) > 128:
        raise ValueError("한 페이지의 보표가 너무 많습니다.")
    connections, gaps = [], []
    for upper, lower in zip(staves, staves[1:]):
        d = float(np.median([upper["spacing"], lower["spacing"]]))
        gap = lower["lines"][0] - upper["lines"][-1]
        aligned = (abs(upper["left"] - lower["left"]) <= d * 2
                   and abs(upper["right"] - lower["right"]) <= d * 2)
        connected = aligned and any(
            abs(x - upper["left"]) <= d * 2
            and y0 <= upper["lines"][0] + d * 0.25
            and y1 >= lower["lines"][-1] - d * 0.25
            for x, y0, y1 in vectors
        )
        connections.append(connected)
        gaps.append(gap / d)
    explicit = [[staves[0]]]
    for connected, staff in zip(connections, staves[1:]):
        if connected:
            explicit[-1].append(staff)
        else:
            explicit.append([staff])
    patterns = set(tuple(p) for p in known_patterns if len(p) > 1)
    patterns.update(tuple(len(s["lines"]) for s in group) for group in explicit if len(group) > 1)
    counts = [len(s["lines"]) for s in staves]
    candidates = [p for p in patterns
                  if len(counts) % len(p) == 0 and counts == list(p) * (len(counts) // len(p))
                  and not any(connections[i - 1] for i in range(len(p), len(counts), len(p)))
                  and all(connections[i] or gaps[i] < 8 for i in range(len(connections))
                          if (i + 1) % len(p))]
    if len(candidates) == 1:
        pattern = candidates[0]
        if len(pattern) > 32:
            raise ValueError("한 연주 줄의 보표는 최대 32개까지 지원합니다.")
        grouped = [staves[i:i + len(pattern)] for i in range(0, len(staves), len(pattern))]
        # Some engravings connect only the first system. Reuse that visible pattern,
        # then require its simultaneous staves to agree on every boundary below.
        for group in grouped:
            d = float(np.median([s["spacing"] for s in group]))
            if max(s["left"] for s in group) - min(s["left"] for s in group) > d * 2:
                raise ValueError("반복 보표 구성의 왼쪽 경계가 일치하지 않습니다.")
        methods = [
            "repeated-connected-pattern"
            if all(connections[start:start + len(pattern) - 1]) else "inferred-connected-pattern"
            for start in range(0, len(staves), len(pattern))
        ]
        return grouped, methods
    if len(candidates) > 1:
        raise ValueError("여러 보표 구성이 가능해 악보의 줄을 구분하지 못했습니다.")
    grouped, methods = [[staves[0]]], []
    for connected, gap, lower in zip(connections, gaps, staves[1:]):
        if connected:
            grouped[-1].append(lower)
            methods.append("left-connector")
        elif gap >= 8:
            grouped.append([lower])
            methods.append("separated-systems")
        else:
            # Close unconnected rows can be either parts or consecutive systems.
            raise ValueError(
                "가까운 보표 사이의 연결선이 불명확해 총보의 줄을 구분하지 못했습니다."
            )
    if any(len(group) > 32 for group in grouped):
        raise ValueError("한 연주 줄의 보표는 최대 32개까지 지원합니다.")
    return grouped, methods


def shared_bar_boundaries(ink, staves, vectors, *, prefer_vectors=True):
    """Choose shared boundaries using independent staff votes, never equal widths."""
    d = float(np.median([s["spacing"] for s in staves]))
    bounds = (staves[0]["lines"][0], staves[-1]["lines"][-1])
    endpoints = []
    for _, y0, y1 in vectors:
        if any(abs(y0 - s["lines"][0]) <= max(1.5, d * 0.2) for s in staves) and any(
            abs(y1 - s["lines"][-1]) <= max(1.5, d * 0.2) for s in staves
        ):
            endpoints.append((y0, y1))
    endpoints = sorted(set(endpoints))
    observations = []
    counts = []
    for index, staff in enumerate(staves):
        spans = [(a, b) for a, b in endpoints
                 if a <= staff["lines"][0] + d * 0.2 and b >= staff["lines"][-1] - d * 0.2]
        positions = barlines(ink, staff, vectors, bounds, connected_bounds=spans,
                             prefer_vectors=prefer_vectors, reject_vector_caps=True)
        counts.append(len(positions) - 1)
        observations.extend((float(x), index) for x in positions)
    observations.sort()
    clusters = []
    for x, staff in observations:
        if not clusters or x - clusters[-1][0][0] > d * 1.6:
            clusters.append([])
        clusters[-1].append((x, staff))
    required = len(staves) // 2 + 1
    accepted = [cluster for cluster in clusters if len({i for _, i in cluster}) >= required]
    if len(accepted) < 2:
        raise ValueError("보표들이 공유하는 마디 경계를 확인하지 못했습니다.")
    xs = [float(np.median([x for x, _ in cluster])) for cluster in accepted]
    support = [len({i for _, i in cluster}) for cluster in accepted]
    if len(xs) == 2 and max(counts) > 1:
        raise ValueError("보표별 마디선 후보가 충돌해 마디 수를 확정하지 못했습니다.")
    left = float(np.median([s["left"] for s in staves]))
    right = float(np.median([s["right"] for s in staves]))
    repeat_header = xs[0] - left < d * 12 and any(
        repeat_dots(ink, staff, xs[0], 1) for staff in staves
    )
    if (abs(xs[0] - left) > d * 2 and not repeat_header) or abs(xs[-1] - right) > d * 2:
        raise ValueError("공유 마디선이 총보의 양쪽 끝과 일치하지 않습니다.")
    if any(np.diff(xs) < d * 2):
        raise ValueError("지나치게 좁은 마디 후보가 있습니다. 마디선을 확인하세요.")

    def observed(x, staff):
        top, bottom = round(staff["lines"][0]), round(staff["lines"][-1])
        column = round(x)
        return ink[top:bottom + 1, max(0, column - 1):column + 2].any(axis=1).mean() > 0.85

    edge_observed = [sum(observed(x, s) for s in staves) >= required for x in (xs[0], xs[-1])]
    if not any(observed(x, s) for x in xs for s in staves):
        raise ValueError("실제 세로 마디선이 없어 수평 보표만으로 마디를 만들 수 없습니다.")
    candidates = [{"x": float(np.median([x for x, _ in cluster])),
                   "support": len({i for _, i in cluster}),
                   "accepted": len({i for _, i in cluster}) >= required} for cluster in clusters]
    reasons = []
    if len(set(counts)) > 1 or any(n < len(staves) for n in support):
        reasons.append("보표별 마디선이 모두 일치하지 않아 다수의 공통 경계를 사용했습니다.")
    if any(not candidate["accepted"] for candidate in candidates):
        reasons.append("공통 경계에 포함하지 않은 마디선 후보가 있습니다.")
    if not edge_observed[-1]:
        reasons.append("오른쪽 끝의 세로 마디선이 불명확합니다.")
    return xs, {"staff_measure_counts": counts, "boundary_support": support,
                "required_support": required, "boundary_candidates": candidates,
                "inferred_edges": {"left": not edge_observed[0], "right": not edge_observed[1]},
                "review_reasons": reasons}


def pdf_only_page_geometry(gray, vectors=(), known_patterns=()):
    """Recover printed full-score systems without an external notation document."""
    ink, staves, angle = straighten(gray, allow_four=True)
    d = float(np.median([s["spacing"] for s in staves]))
    original_vectors = list(vectors) if angle == 0 else []
    raster = raster_verticals(ink, d)
    grouped, methods = group_pdf_staves(ink, staves, original_vectors + raster, known_patterns)
    # Single-staff raster strokes still need barlines()'s notehead/beam rejection.
    spanning = [segment for segment in raster if sum(
        segment[1] <= s["lines"][0] + d * 0.2
        and segment[2] >= s["lines"][-1] - d * 0.2 for s in staves
    ) >= 2]
    segments = original_vectors + spanning
    systems = []
    for index, group in enumerate(grouped):
        xs, votes = shared_bar_boundaries(ink, group, segments,
                                          prefer_vectors=bool(original_vectors))
        if methods and methods[0] in (
            "repeated-connected-pattern", "inferred-connected-pattern"
        ) and any(
            count != len(xs) - 1 for count in votes["staff_measure_counts"]
        ):
            raise ValueError("반복 보표 구성의 마디선 수가 일치하지 않습니다.")
        if (
            "inferred-connected-pattern" in methods
            and methods[index] == "inferred-connected-pattern"
        ):
            votes["review_reasons"].append(
                "앞서 확인한 보표 구성으로 연결선 없는 보표를 묶었습니다. "
                "같은 연주 줄인지 확인하세요."
            )
        if any(len(staff["lines"]) == 4 for staff in group):
            votes["review_reasons"].append(
                "4선 보표의 선 수와 흐리거나 빠진 선이 없는지 확인하세요."
            )
        if "separated-systems" in methods:
            votes["review_reasons"].append("보표 사이 간격으로 구분한 연주 줄을 확인하세요.")
        spacing = float(np.median([s["spacing"] for s in group]))
        top = group[0]["lines"][0] - spacing * 4
        bottom = group[-1]["lines"][-1] + spacing * 5
        if index:
            top = max(top, (grouped[index - 1][-1]["lines"][-1] + group[0]["lines"][0]) / 2)
        if index + 1 < len(grouped):
            bottom = min(bottom, (group[-1]["lines"][-1] + grouped[index + 1][0]["lines"][0]) / 2)
        box = normalized_box(xs[0], top, xs[-1], bottom, angle, ink.shape)
        bars = []
        for left, right in zip(xs, xs[1:]):
            bar = normalized_box(left, top, right, bottom, angle, ink.shape)
            bar.update(
                fraction0=float((left - xs[0]) / (xs[-1] - xs[0])),
                fraction1=float((right - xs[0]) / (xs[-1] - xs[0])),
                repeat_start=any(repeat_dots(ink, s, left, 1) for s in group),
                repeat_end=any(repeat_dots(ink, s, right, -1) for s in group),
            )
            if angle == 0:
                bar.update(staff_top=group[0]["lines"][0] / ink.shape[0],
                           staff_spacing=spacing / ink.shape[0],
                           staff_spacing_x=spacing / ink.shape[1])
            bars.append(bar)
        systems.append({"box": box, "bars": bars, "tab_companion": False,
                        "staff_pattern": [len(s["lines"]) for s in group], **votes})
    return systems, angle, methods


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


def detect_pdf_layout(pdf, xml=None, report=lambda *_: None):
    import pypdfium2

    pattern, expected = xml_staff_pattern(xml) if xml is not None else (None, None)
    sizes, bars, pages, known_patterns, review_required = [], {}, [], set(), []
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
                    vectors = vector_verticals(page, bitmap)
                    if pattern is None:
                        systems, angle, grouping = pdf_only_page_geometry(
                            gray, vectors, known_patterns
                        )
                        known_patterns.update(tuple(s["staff_pattern"]) for s in systems)
                    else:
                        systems, angle = page_geometry(gray, pattern, vectors)
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
                        "tab_companion": any(s["tab_companion"] for s in systems),
                    }
                )
                if pattern is None:
                    pages[-1].update(
                        staff_patterns=[s["staff_pattern"] for s in systems],
                        grouping=grouping,
                        boundary_support=[s["boundary_support"] for s in systems],
                        staff_measure_counts=[s["staff_measure_counts"] for s in systems],
                        boundary_candidates=[s["boundary_candidates"] for s in systems],
                        inferred_edges=[s["inferred_edges"] for s in systems],
                    )
                    review_required.extend(
                        {"page": number, "system": index, "reasons": system["review_reasons"]}
                        for index, system in enumerate(systems) if system["review_reasons"]
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
                        if len(bars) > 2000:
                            raise ValueError("자동 배치는 최대 2,000마디까지 지원합니다.")
            finally:
                page.close()
    if expected is not None and len(bars) != expected:
        raise ValueError(
            f"PDF 마디선 {len(bars)}마디와 MusicXML {expected}마디가 일치하지 않습니다. "
            "복합 쉼표·숨긴 파트·인식 결과를 확인하세요."
        )
    return (
        sizes,
        bars,
        {
            "version": LAYOUT_VERSION,
            "method": "pdf-staves-barlines" if xml is not None else "pdf-only-staves-barlines",
            "pages": pages,
            "measure_count": len(bars),
            **({"warnings": ["PDF의 인쇄된 마디 배치만 인식했습니다. "
                              "음표·TAB 음고·음가는 인식하지 않습니다."] + [
                                  f"{item['page'] + 1}페이지 {item['system'] + 1}번째 줄: "
                                  + " ".join(item["reasons"]) for item in review_required
                              ], "review_required": review_required}
               if xml is None else {}),
        },
    )
