# SPDX-License-Identifier: GPL-3.0-only
"""Validated display geometry and saved camera paths, without OMR dependencies."""

import math

SCROLL_VERSION = 1


def view_profile(zoom, width, height):
    if zoom not in ("page", "width"):
        zoom = float(zoom)
        if not math.isfinite(zoom) or not 0.5 <= zoom <= 6:
            raise ValueError("악보 배율은 50~600%여야 합니다.")
    width, height = int(width), int(height)
    if not 100 <= width <= 12000 or not 100 <= height <= 12000:
        raise ValueError("악보 영역의 너비·높이는 100~12000 픽셀이어야 합니다.")
    return {"zoom": zoom, "width": width, "height": height}


def rendered_size(page_size, profile):
    width, height = page_size
    scale = (profile["width"] - 8) / width
    if profile["zoom"] == "page":
        scale = min(scale, (profile["height"] - 8) / height)
    elif profile["zoom"] != "width":
        scale = profile["zoom"]
    return max(1, round(width * scale)), max(1, round(height * scale))


def validate_scroll(data, cues, page_count):
    if data.get("version") != SCROLL_VERSION:
        raise ValueError("스크롤 분석 버전이 다릅니다. 다시 분석하세요.")
    profile = view_profile(**data["profile"])
    sizes, frames = data["page_sizes"], data["frames"]
    if len(sizes) != page_count or len(frames) != len(cues):
        raise ValueError("스크롤 분석과 악보 시간표의 길이가 다릅니다.")
    for size in sizes:
        if len(size) != 2 or any(not math.isfinite(x) or x <= 0 for x in size):
            raise ValueError("잘못된 PDF 페이지 크기입니다.")
    for cue, frame in zip(cues, frames):
        if frame["page"] != cue["page"] or frame["source_index"] != cue["source_index"]:
            raise ValueError("페이지/마디 변경 후 스크롤을 다시 분석하세요.")
        width, height = rendered_size(sizes[cue["page"]], profile)
        for key in ("x0", "x1", "y0", "y1", "intro_y"):
            limit = max(
                0, (width - profile["width"]) if key[0] == "x" else (height - profile["height"])
            )
            value = frame[key]
            if not math.isfinite(value) or not 0 <= value <= limit + 1:
                raise ValueError("스크롤 위치가 페이지 범위를 벗어났습니다.")
        if not 0 <= frame["settle"] <= 0.5:
            raise ValueError("스크롤 전환 길이가 올바르지 않습니다.")


def camera_at(frame, fraction):
    fraction = min(1, max(0, fraction))
    x = frame["x0"] + (frame["x1"] - frame["x0"]) * fraction
    settle = frame["settle"]
    if settle and fraction < settle:
        t = fraction / settle
        t = t * t * (3 - 2 * t)
        y = frame["intro_y"] + (frame["y0"] - frame["intro_y"]) * t
    else:
        t = (fraction - settle) / (1 - settle)
        y = frame["y0"] + (frame["y1"] - frame["y0"]) * t
    return round(x), round(y)
