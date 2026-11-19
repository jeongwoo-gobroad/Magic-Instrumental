# SPDX-License-Identifier: GPL-3.0-only
"""Conservative PDF text/repeat-sign supplements to OMR, before route expansion."""

import re
from xml.etree.ElementTree import SubElement

from defusedxml import ElementTree as ET

COMMAND = re.compile(r"\bD\s*\.?\s*([CS])\b\s*\.?(?:\s*al\s+(Coda|Fine)\b)?", re.I)
MARKS = re.compile(r"\bTo\s+Coda\b|\bSegno\b|\bCoda\b|\bFine\b|[\U0001d10b\U0001d10c]", re.I)


def word_marks(text):
    covered = []
    found = []
    for match in COMMAND.finditer(text):
        kind = "dacapo" if match[1].upper() == "C" else "dalsegno"
        found.append((kind, match.start(), match.end()))
        covered.append((match.start(), match.end()))
    for match in MARKS.finditer(text):
        if any(a <= match.start() < b for a, b in covered):
            continue
        label = match[0].lower()
        kind = (
            "tocoda"
            if label.startswith("to")
            else "segno"
            if label in ("segno", "\U0001d10b")
            else "coda"
            if label in ("coda", "\U0001d10c")
            else "fine"
        )
        found.append((kind, match.start(), match.end()))
    return sorted(found, key=lambda x: x[1])


def text_marks(pdf, *, include_numbers=False):
    import pypdfium2
    import pypdfium2.raw as raw

    found = []
    with pypdfium2.PdfDocument(str(pdf)) as document:
        for page_number in range(len(document)):
            page = document[page_number]
            bitmap = page.render(scale=min(1, 2000 / max(page.get_size())))
            text = page.get_textpage()
            try:
                count = text.count_chars()
                if count > 100000:
                    raise ValueError("PDF 텍스트가 너무 많습니다.")
                converter = bitmap.get_posconv(page)
                chars = []
                for i in range(count):
                    code = raw.FPDFText_GetUnicode(text, i)
                    if not code or code > 0x10FFFF or chr(code).isspace():
                        continue
                    left, bottom, right, top = text.get_charbox(i)
                    a, b = converter.to_bitmap(left, top), converter.to_bitmap(right, bottom)
                    x0, x1 = sorted((a[0], b[0]))
                    y0, y1 = sorted((a[1], b[1]))
                    if x1 > x0 and y1 > y0:
                        chars.append((chr(code), x0, x1, (y0 + y1) / 2, y1 - y0))
                lines = []
                for char in sorted(chars, key=lambda c: (c[3], c[1])):
                    # Nearby character centers form a baseline; separate distant columns.
                    if not lines or abs(char[3] - lines[-1][0][3]) > max(2, char[4] * 0.45):
                        lines.append([])
                    lines[-1].append(char)
                for line in lines:
                    runs = [[]]
                    for char in sorted(line, key=lambda c: c[1]):
                        if runs[-1] and char[1] - runs[-1][-1][2] > max(8, char[4] * 2):
                            runs.append([])
                        runs[-1].append(char)
                    for run in runs:
                        value, coordinates = "", []
                        for char in run:
                            if coordinates and char[1] - coordinates[-1][2] > char[4] * 0.3:
                                value += " "
                                coordinates.append(coordinates[-1])
                            value += char[0]
                            coordinates.append(char)
                        marks = word_marks(value)
                        if include_numbers:
                            marks.extend(("measure-number", m.start(), m.end())
                                         for m in re.finditer(r"\b\d(?:\s*\d){0,3}\b", value))
                        for kind, start, end in marks:
                            selected = coordinates[start:end]
                            found.append(
                                {
                                    "page": page_number,
                                    "kind": kind,
                                    "text": value[start:end],
                                    "x": selected[0][1] / bitmap.width,
                                    "y": sum(c[3] for c in selected)
                                    / len(selected)
                                    / bitmap.height,
                                }
                            )
            finally:
                text.close()
                bitmap.close()
                page.close()
    return found


def printed_measure_numbers(pdf, bars):
    """Only isolated numbers just above the staff at a verified left barline."""
    candidates = {}
    for mark in text_marks(pdf, include_numbers=True):
        if mark["kind"] != "measure-number":
            continue
        matches = []
        for index, box in bars.items():
            if box["page"] != mark["page"] or "staff_top" not in box:
                continue
            distance = (box["staff_top"] - mark["y"]) / box["staff_spacing"]
            dx = (mark["x"] - box["left"]) / box["staff_spacing_x"]
            if 0.25 <= distance <= 3.5 and -0.7 <= dx <= 1.5:
                matches.append(index)
        if len(matches) == 1:
            candidates.setdefault(matches[0], set()).add("".join(mark["text"].split()))
    return {i: next(iter(values)) for i, values in candidates.items() if len(values) == 1}


def locate_mark(mark, bars):
    systems = {}
    for index, bar in bars.items():
        if bar["page"] == mark["page"]:
            systems.setdefault(bar["system"], []).append((index, bar))
    candidates = []
    for row in systems.values():
        top = min(b["top"] for _, b in row)
        bottom = max(b["bottom"] for _, b in row)
        distance = max(top - mark["y"], mark["y"] - bottom, 0)
        candidates.append((distance, row))
    candidates.sort(key=lambda x: x[0])
    if not candidates or candidates[0][0] > 0.04:
        return None
    if len(candidates) > 1 and abs(candidates[0][0] - candidates[1][0]) < 0.005:
        return None
    row = candidates[0][1]
    x = mark["x"]
    if x < row[0][1]["left"] - 0.04 or x > row[-1][1]["right"] + 0.04:
        return None
    # Commands at a right barline belong to the preceding bar; destination signs
    # at a left barline belong to the following one.
    if mark["kind"] in {"dacapo", "dalsegno", "fine", "tocoda"}:
        return next((i for i, b in row if x <= b["right"] + 0.004), row[-1][0])
    return next((i for i, b in row if x < b["right"] - 0.004), row[-1][0])


def supplement_xml(pdf, xml, bars):
    from .score_analysis import read_musicxml

    root = ET.fromstring(read_musicxml(xml))
    for node in root.iter():
        node.tag = node.tag.split("}")[-1]
    measures = root.find("part").findall("measure")
    additions, warnings = [], []
    candidates = text_marks(pdf)
    for index, box in bars.items():
        for flag, kind in (("repeat_start", "forward"), ("repeat_end", "backward")):
            if box.get(flag):
                candidates.append({"index": index, "kind": kind})
    for mark in candidates:
        index = mark.get("index") if "index" in mark else locate_mark(mark, bars)
        kind = mark["kind"]
        if index is None:
            warnings.append(f"PDF의 {kind} 기호 위치가 모호해 자동 보충하지 않았습니다.")
            continue
        measure = measures[index]
        if kind in {"forward", "backward"}:
            if any(x.get("direction") == kind for x in measure.findall("barline/repeat")):
                continue
            side = "left" if kind == "forward" else "right"
            line = next((x for x in measure.findall("barline") if x.get("location") == side), None)
            if line is None:
                line = SubElement(measure, "barline", location=side)
            SubElement(line, "repeat", direction=kind)
        else:
            existing = {key for sound in measure.iter("sound") for key in sound.attrib}
            existing.update(node.tag for node in measure.iter() if node.tag in {"segno", "coda"})
            for words in measure.iter("words"):
                existing.update(k for k, _, _ in word_marks(words.text or ""))
            if kind in existing:
                continue
            if kind in {"dacapo", "dalsegno"} and existing & {"dacapo", "dalsegno"}:
                raise ValueError("PDF와 MusicXML의 D.C./D.S.가 충돌합니다. 연주 순서를 확인하세요.")
            direction = SubElement(measure, "direction", placement="above")
            dtype = SubElement(direction, "direction-type")
            words = SubElement(dtype, "words")
            command = COMMAND.fullmatch(mark.get("text", ""))
            if command and command[2]:
                stem = "D.C." if kind == "dacapo" else "D.S."
                words.text = stem + " al " + command[2].title()
            value = "yes" if kind in {"dacapo", "fine"} else "pdf-navigation"
            SubElement(direction, "sound", {kind: value})
        additions.append(
            {
                "measure": index + 1,
                "kind": kind,
                "source": "pdf-repeat-dots" if "index" in mark else "pdf-text",
            }
        )
    if additions:
        warnings.append(
            f"PDF에서 누락된 이동/반복 기호 {len(additions)}개를 보충했습니다. 확인하세요."
        )
    return ET.tostring(root, encoding="utf-8"), additions, warnings
