# SPDX-License-Identifier: GPL-3.0-only
"""Offline PDF staff detection and zoom-specific camera planning."""

import json
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import numpy as np
from defusedxml import ElementTree as ET
from filelock import FileLock

from .cache import hash_file
from .score import ScoreTimeline, atomic_json, matching_musicxml
from .score_view import SCROLL_VERSION, rendered_size, view_profile


def printed_systems(xml):
    from .score_analysis import read_musicxml

    root = ET.fromstring(read_musicxml(xml))
    for node in root.iter():
        node.tag = node.tag.split("}")[-1]
    parts = root.findall("part")
    if not parts:
        raise ValueError("악보 파트를 찾지 못했습니다.")
    measures = parts[0].findall("measure")
    systems, page = [], 0
    for index, measure in enumerate(measures):
        prints = measure.findall("print")
        new_page = index > 0 and any(x.get("new-page") == "yes" for x in prints)
        if new_page:
            page += 1
        if not index or new_page or any(x.get("new-system") == "yes" for x in prints):
            systems.append({"page": page, "measures": [], "line_counts": []})
        width = float(measure.get("width", 0))
        if not np.isfinite(width) or width <= 0:
            raise ValueError(
                "MusicXML에 마디 폭이 없습니다. PDF를 Audiveris로 분석한 "
                "MusicXML 또는 인쇄 배치가 포함된 MusicXML을 사용하세요."
            )
        systems[-1]["measures"].append((index, width))
    # Number of simultaneously printed staves can change at system boundaries.
    for part in parts:
        part_measures = part.findall("measure")
        if len(part_measures) != len(measures):
            raise ValueError("파트별 마디 수가 달라 악보 줄을 연결할 수 없습니다.")
        staves, lines = 1, {}
        system_by_start = {s["measures"][0][0]: s for s in systems}
        for index, measure in enumerate(part_measures):
            for attrs in measure.findall("attributes"):
                staves = int(attrs.findtext("staves", staves))
                for details in attrs.findall("staff-details"):
                    number = int(details.get("number", 1))
                    lines[number] = int(details.findtext("staff-lines", lines.get(number, 5)))
            if index in system_by_start:
                if not 1 <= staves <= 32:
                    raise ValueError("악보의 보표 수를 확인하세요.")
                system_by_start[index]["line_counts"].extend(
                    lines.get(n, 5) for n in range(1, staves + 1)
                )
    return systems


def staff_boxes(gray, line_counts):
    """Find long, regularly spaced staff lines. Reject inconsistent page layouts."""
    height, width = gray.shape
    ink = gray < 110
    rows = np.flatnonzero(ink.sum(axis=1) > width * 0.40)
    groups = np.split(rows, np.flatnonzero(np.diff(rows) > 1) + 1)
    lines = [float(np.mean(g)) for g in groups if len(g)]
    expected = sum(line_counts)
    if any(n not in (5, 6) for n in line_counts) or len(lines) != expected:
        raise ValueError(
            f"악보 줄 위치를 확정하지 못했습니다 (긴 선 {len(lines)}개, "
            f"MusicXML 예상 {expected}개). 같은 판본·줄바꿈을 확인하세요. "
            "현재 자동 스크롤은 선명한 5선/6선 보표를 지원합니다."
        )
    boxes, offset = [], 0
    for count in line_counts:
        staff = lines[offset : offset + count]
        offset += count
        gaps = np.diff(staff)
        spacing = float(np.median(gaps))
        if (
            spacing < 2
            or spacing > height * 0.03
            or np.max(np.abs(gaps - spacing)) > max(2, spacing * 0.25)
        ):
            raise ValueError("오선 간격이 불규칙합니다. 기울어진 스캔 또는 줄바꿈을 확인하세요.")
        row_ink = np.mean(ink[[round(y) for y in staff]], axis=0)
        columns = np.flatnonzero(row_ink >= 0.8)
        if not len(columns):
            raise ValueError("오선의 가로 범위를 인식하지 못했습니다.")
        boxes.append(
            {
                "left": float(columns[0]) / width,
                "right": float(columns[-1]) / width,
                "top": max(0, staff[0] - 2.5 * spacing) / height,
                "bottom": min(height, staff[-1] + 3.5 * spacing) / height,
            }
        )
    return boxes


def detect_layout(pdf, xml, report=lambda *_: None):
    import pypdfium2

    systems = printed_systems(xml)
    page_sizes, bars = [], {}
    with pypdfium2.PdfDocument(str(pdf)) as document:
        if max(s["page"] for s in systems) + 1 != len(document):
            raise ValueError("PDF와 MusicXML의 페이지 수가 다릅니다. 동일 판본으로 분석하세요.")
        for number in range(len(document)):
            report(f"확대 스크롤 사전 분석 · {number + 1}/{len(document)}페이지 줄 위치 확인", None)
            page = document[number]
            size = page.get_size()
            page_sizes.append(list(size))
            bitmap = page.render(scale=min(2, 2400 / max(size)))
            gray = np.asarray(bitmap.to_pil().convert("L"))
            rows = [s for s in systems if s["page"] == number]
            if not rows:
                raise ValueError(
                    "마디가 없는 페이지가 있습니다. 같은 판본의 MusicXML을 사용하세요."
                )
            boxes = staff_boxes(gray, [n for s in rows for n in s["line_counts"]])
            bitmap.close()
            page.close()
            cursor = 0
            for system_index, system in enumerate(rows):
                group = boxes[cursor : cursor + len(system["line_counts"])]
                cursor += len(group)
                system["box"] = {
                    "left": min(x["left"] for x in group),
                    "right": max(x["right"] for x in group),
                    "top": group[0]["top"],
                    "bottom": group[-1]["bottom"],
                }
                total = sum(width for _, width in system["measures"])
                running = 0
                for index, width in system["measures"]:
                    box = system["box"]
                    bars[index] = {
                        "page": number,
                        "system": system_index,
                        "top": box["top"],
                        "bottom": box["bottom"],
                        "left": box["left"] + running / total * (box["right"] - box["left"]),
                        "right": box["left"]
                        + (running + width) / total * (box["right"] - box["left"]),
                        "fraction0": running / total,
                        "fraction1": (running + width) / total,
                    }
                    running += width
            for system_index, system in enumerate(rows):
                next_top = (
                    rows[system_index + 1]["box"]["top"]
                    if system_index + 1 < len(rows)
                    else system["box"]["top"]
                )
                for index, _ in system["measures"]:
                    bars[index]["next_top"] = next_top
    return page_sizes, bars


def plan_camera(cues, end, page_sizes, bars, profile):
    frames = []
    for index, cue in enumerate(cues):
        bar = bars[cue["source_index"]]
        if bar["page"] != cue["page"]:
            raise ValueError(
                "보정한 시간표의 페이지와 PDF 줄 위치가 다릅니다. 원본 배치를 확인하세요."
            )
        width, height = rendered_size(page_sizes[cue["page"]], profile)

        def clip(value, axis):
            maximum = max(
                0, (width - profile["width"]) if axis == "x" else (height - profile["height"])
            )
            return round(min(maximum, max(0, value)), 4)

        y0 = clip(bar["top"] * height - profile["height"] * 0.45, "y")
        y1 = max(
            y0,
            clip(
                min(
                    bar["next_top"] * height - profile["height"] * 0.45,
                    bar["top"] * height - profile["height"] * 0.10,
                ),
                "y",
            ),
        )
        frame = {
            "page": cue["page"],
            "source_index": cue["source_index"],
            "system": bar["system"],
            "x0": clip(bar["left"] * width - profile["width"] * 0.45, "x"),
            "x1": clip(bar["right"] * width - profile["width"] * 0.45, "x"),
            "y0": y0 + (y1 - y0) * bar["fraction0"],
            "y1": y0 + (y1 - y0) * bar["fraction1"],
            "settle": 0,
        }
        frame["intro_y"] = frame["y0"]
        if (
            index
            and frames[-1]["page"] == frame["page"]
            and cues[index - 1]["source_index"] + 1 == cue["source_index"]
            and frames[-1]["system"] != frame["system"]
        ):
            duration = (cues[index + 1]["start"] if index + 1 < len(cues) else end) - cue["start"]
            frame["intro_y"] = frames[-1]["y1"]
            frame["settle"] = min(0.2, 0.35 / duration)
        frames.append(frame)
    return {
        "version": SCROLL_VERSION,
        "profile": profile,
        "page_sizes": page_sizes,
        "frames": frames,
        "layout_method": "pdf-staff-projection-musicxml-widths-v1",
    }


def analyze_scroll(audio, pdf, paths, timeline_path, profile, report=lambda *_: None):
    profile = view_profile(**profile)
    timeline_hash = hash_file(timeline_path)
    base = ScoreTimeline.load(timeline_path)
    audio_hash, pdf_hash = hash_file(audio), hash_file(pdf)
    if base.data["audio_sha256"] != audio_hash or base.data["pdf_sha256"] != pdf_hash:
        raise ValueError("음원·PDF와 기존 시간표가 다릅니다. 악보 전체 분석을 먼저 실행하세요.")
    folder = paths.processed / audio_hash / "scores" / pdf_hash
    candidates = [
        matching_musicxml(pdf),
        Path(base.data.get("musicxml_path", "")),
        folder / "recognition/reviewed.musicxml",
        folder / "recognition/score.mxl",
    ]
    xml = next(
        (
            p
            for p in candidates
            if p and p.is_file() and hash_file(p) == base.data.get("xml_sha256")
        ),
        None,
    )
    if xml is None:
        raise ValueError("기존 분석과 같은 MusicXML이 필요합니다. 악보 전체 분석을 실행하세요.")
    data = deepcopy(base.data)
    for name in ("scroll", "view_signature"):
        data.pop(name, None)
    digest = sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    signature = {"version": SCROLL_VERSION, "timing": digest, "profile": profile}
    key = "view-" + sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()[:20]
    destination = folder / key / "timeline.json"
    with FileLock(str(folder / ".analysis.lock"), timeout=3600):
        if destination.is_file():
            try:
                cached = ScoreTimeline.load(destination)
                if cached.data.get("view_signature") == signature:
                    atomic_json(folder / "latest.json", {"analysis": key})
                    report("같은 배율·악보 영역의 스크롤 분석을 불러왔습니다.", 1)
                    return destination
            except (ValueError, KeyError, TypeError, OSError):
                pass
        sizes, bars = detect_layout(pdf, xml, report)
        data["scroll"] = plan_camera(data["cues"], data["end"], sizes, bars, profile)
        data["view_signature"] = signature
        if (
            hash_file(audio) != audio_hash
            or hash_file(pdf) != pdf_hash
            or hash_file(xml) != data["xml_sha256"]
            or hash_file(timeline_path) != timeline_hash
        ):
            raise ValueError("스크롤 분석 중 원본 파일이 변경되었습니다. 다시 분석하세요.")
        ScoreTimeline(data).save(destination)
        atomic_json(folder / "latest.json", {"analysis": key})
        report("확대 배율에 맞춘 화면 이동 경로를 저장했습니다.", 1)
        return destination
