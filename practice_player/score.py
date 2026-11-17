# SPDX-License-Identifier: GPL-3.0-only
"""Small, inference-free playback schedule and content-addressed score associations."""

import json
import math
import os
import unicodedata
import uuid
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from pathlib import Path

from .cache import hash_file
from .config import Paths
from .score_view import camera_at, validate_scroll

SCORE_VERSION = 1


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{uuid.uuid4().hex}.json")
    try:
        with temp.open("w", encoding="utf-8") as output:
            json.dump(data, output, ensure_ascii=False, indent=2, allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def name_key(path: Path) -> str:
    return unicodedata.normalize("NFC", path.stem).casefold()


def matching_score(audio: Path, paths: Paths, source_hash: str = "") -> Path | None:
    if source_hash:
        try:
            info = json.loads((paths.processed / source_hash / "score-link.json").read_text())
            candidate = paths.root / info["pdf"]
            if candidate.is_file():
                return candidate.resolve()
        except (OSError, ValueError, KeyError, TypeError):
            pass
    matches = [
        p
        for p in paths.score_targets.rglob("*")
        if p.suffix.lower() == ".pdf" and name_key(p) == name_key(audio)
    ]
    return matches[0].resolve() if len(matches) == 1 else None


def associate_score(pdf: Path, paths: Paths, source_hash: str) -> None:
    pdf = pdf.resolve(strict=True)
    try:
        location = str(pdf.relative_to(paths.root.resolve()))
    except ValueError:
        location = str(pdf)
    atomic_json(paths.processed / source_hash / "score-link.json", {"pdf": location})


def matching_musicxml(pdf: Path) -> Path | None:
    candidates = [
        p
        for p in pdf.parent.iterdir()
        if p.suffix.lower() in {".musicxml", ".mxl", ".xml"} and name_key(p) == name_key(pdf)
    ]
    return candidates[0] if len(candidates) == 1 else None


@dataclass
class ScoreTimeline:
    data: dict

    def __post_init__(self):
        if self.data.get("version") != SCORE_VERSION:
            raise ValueError("악보 캐시 버전이 다릅니다. 다시 분석하세요.")
        self.cues = self.data["cues"]
        if not math.isfinite(float(self.data.get("offset", 0))):
            raise ValueError("악보 보정 시간은 유한한 값이어야 합니다.")
        self.starts = []
        previous = -1.0
        for cue in self.cues:
            start = float(cue["start"])
            page = cue["page"]
            if not math.isfinite(start) or start < 0 or start <= previous:
                raise ValueError("마디 시작 시간은 0 이상이고 순서대로 증가해야 합니다.")
            if not isinstance(page, int) or not 0 <= page < self.data["page_count"]:
                raise ValueError("악보 페이지가 PDF 범위를 벗어났습니다.")
            self.starts.append(start)
            previous = start
        if not self.starts:
            raise ValueError("악보의 마디 시간표가 비어 있습니다.")
        self.end = float(self.data["end"])
        if not math.isfinite(self.end) or self.end <= self.starts[-1]:
            raise ValueError("악보 종료 시간은 마지막 마디 시작보다 뒤여야 합니다.")
        if self.data.get("scroll") is not None:
            validate_scroll(self.data["scroll"], self.cues, self.data["page_count"])

    def index_at(self, media_seconds: float, offset: float = 0.0) -> int | None:
        # QMediaPlayer position is media time: NEVER multiply by playbackRate.
        moment = media_seconds - offset
        index = bisect_right(self.starts, moment) - 1
        return index if index >= 0 and moment < self.end else None

    def playable_measures(self, duration: float) -> tuple[int, int] | None:
        """Inclusive visit indexes whose measures overlap the playable audio."""
        offset = float(self.data.get("offset", 0))
        if duration <= 0 or self.end + offset <= 0 or self.starts[0] + offset >= duration:
            return None
        first = max(0, bisect_right(self.starts, -offset) - 1)
        last = min(len(self.starts) - 1, bisect_left(self.starts, duration - offset) - 1)
        return (first, last) if first <= last else None

    def measure_target(self, media_seconds: float, delta: int, duration: float):
        """Move by performed measures, including repeated visits, to a measure start.

        Intro/outro are virtual positions immediately before/after the score.
        Results are rounded UP to media milliseconds so a second click does not
        accidentally identify the previous measure at a sub-millisecond boundary.
        """
        limits = self.playable_measures(duration)
        if limits is None:
            return None
        first, last = limits
        offset = float(self.data.get("offset", 0))
        current = bisect_right(self.starts, media_seconds - offset) - 1
        if media_seconds >= min(duration, self.end + offset):
            current = last + 1
        current = max(first - 1, min(last + 1, current))
        target = max(first, min(last, current + delta))
        milliseconds = min(
            round(duration * 1000), max(0, math.ceil((self.starts[target] + offset) * 1000))
        )
        return target, milliseconds

    def save(self, path: Path) -> None:
        ScoreTimeline(self.data)
        atomic_json(path, self.data)

    def scroll_at(self, media_seconds: float):
        index = self.index_at(media_seconds, self.data.get("offset", 0))
        if index is None or not self.data.get("scroll"):
            return None
        end = self.starts[index + 1] if index + 1 < len(self.starts) else self.end
        fraction = (media_seconds - self.data.get("offset", 0) - self.starts[index]) / (
            end - self.starts[index]
        )
        frame = self.data["scroll"]["frames"][index]
        return frame["page"], *camera_at(frame, fraction)

    @classmethod
    def load(cls, path: Path):
        return cls(json.loads(path.read_text(encoding="utf-8")))


def load_last_timeline(
    pdf: Path, paths: Paths, source_hash: str
) -> tuple[Path, ScoreTimeline] | None:
    try:
        folder = paths.processed / source_hash / "scores" / hash_file(pdf)
        pointer = json.loads((folder / "latest.json").read_text())
        name = pointer["analysis"]
        if Path(name).name != name:
            return None
        path = folder / name / "timeline.json"
        plan = ScoreTimeline.load(path)
        if plan.data["audio_sha256"] != source_hash or plan.data["pdf_sha256"] != folder.name:
            return None
        return path, plan
    except (OSError, ValueError, TypeError, KeyError):
        return None
