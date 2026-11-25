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

from filelock import FileLock, Timeout

from .cache import hash_file
from .config import Paths
from .score_view import camera_at, validate_scroll

SCORE_VERSION = 1
AUTO_ANALYSIS_VERSION = 3
_RECOVERY_SCAN_LIMIT = 256


def stale_pdf_fallback(data: dict) -> bool:
    """Old automatic estimates may have discarded a valid repeated route."""
    signature = data.get("signature", {})
    if not isinstance(signature, dict):
        return False
    automatic = signature.get("automatic")
    return bool(
        data.get("analysis_fallback")
        and signature.get("mode") == "auto"
        and not signature.get("route")
        and (not isinstance(automatic, dict)
             or automatic.get("version") != AUTO_ANALYSIS_VERSION)
    )


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
            link = paths.processed / source_hash / "score-link.json"
            info = json.loads(link.read_text(encoding="utf-8"))
            candidate = paths.root / info["pdf"]
            if candidate.is_file():
                return candidate.resolve()
        except (OSError, ValueError, KeyError, TypeError):
            pass
    matches = [
        p
        for p in paths.score_targets.rglob("*")
        if p.suffix.lower() in {".pdf", ".gp3", ".gp4", ".gp5"} and name_key(p) == name_key(audio)
    ]
    return matches[0].resolve() if len(matches) == 1 else None


def associate_score(pdf: Path, paths: Paths, source_hash: str, track=1) -> None:
    pdf = pdf.resolve(strict=True)
    try:
        location = str(pdf.relative_to(paths.root.resolve()))
    except ValueError:
        location = str(pdf)
    atomic_json(
        paths.processed / source_hash / "score-link.json", {"pdf": location, "track": track}
    )


def matching_musicxml(pdf: Path) -> Path | None:
    candidates = [
        p
        for p in pdf.parent.iterdir()
        if p.suffix.lower() in {".musicxml", ".mxl", ".xml", ".gp3", ".gp4", ".gp5"}
        and name_key(p) == name_key(pdf)
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

    def measure_range(self, first: int, last: int, duration: float) -> tuple[int, int]:
        """Inclusive performed measures -> [start, end) in original-media ms."""
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("음원 길이가 올바르지 않습니다.")
        limits = self.playable_measures(duration)
        if (
            type(first) is not int
            or type(last) is not int
            or limits is None
            or not limits[0] <= first <= last <= limits[1]
        ):
            raise ValueError("시작·끝 마디를 음원 범위 안에서 순서대로 선택하세요.")
        offset = float(self.data.get("offset", 0))
        end = self.starts[last + 1] if last + 1 < len(self.starts) else self.end
        start_ms = max(0, math.ceil((self.starts[first] + offset) * 1000))
        end_ms = min(round(duration * 1000), math.ceil((end + offset) * 1000))
        if start_ms >= end_ms:
            raise ValueError("선택한 마디 구간이 너무 짧거나 음원 범위 밖입니다.")
        return start_ms, end_ms

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


def _paired_timeline(path, source_hash, pdf_hash):
    """A previous result is eligible only for the identical input bytes."""
    try:
        if path.is_symlink():
            return None
        plan = ScoreTimeline.load(path)
        if plan.data["audio_sha256"] != source_hash or plan.data["pdf_sha256"] != pdf_hash:
            return None
        return path, plan
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def _previous_reviewed_timeline(folder, current, source_hash):
    candidates = []
    try:
        with os.scandir(folder) as entries:
            for count, entry in enumerate(entries):
                if count >= _RECOVERY_SCAN_LIMIT:
                    return None
                if not entry.is_dir(follow_symlinks=False):
                    continue
                path = Path(entry.path) / "timeline.json"
                if path == current or path.is_symlink():
                    continue
                try:
                    candidates.append((path.stat().st_mtime_ns, path))
                except OSError:
                    continue
    except OSError:
        return None
    for _, path in sorted(candidates, reverse=True):
        candidate = _paired_timeline(path, source_hash, folder.name)
        if candidate is not None:
            data = candidate[1].data
            if data.get("reviewed") is True and not data.get("analysis_fallback"):
                return candidate
    return None


def load_last_timeline(
    pdf: Path, paths: Paths, source_hash: str, *, repair_pointer=True,
) -> tuple[Path, ScoreTimeline] | None:
    paths.require_cache_locations()
    try:
        folder = paths.score_cache / source_hash / "scores" / hash_file(pdf)
        pointer_path = folder / "latest.json"
        pointer_bytes = pointer_path.read_bytes()
        name = json.loads(pointer_bytes)["analysis"]
        if Path(name).name != name or name in {"", ".", ".."}:
            return None
        path = folder / name / "timeline.json"
        selected = _paired_timeline(path, source_hash, folder.name)
        if selected is None:
            return None
        plan = selected[1]
        if not plan.data.get("reviewed") and stale_pdf_fallback(plan.data):
            previous = _previous_reviewed_timeline(folder, path, source_hash)
            if (previous is not None and pointer_path.read_bytes() == pointer_bytes
                    and hash_file(pdf) == folder.name):
                # 이전 보정본의 내용은 그대로 두고, 잘못된 자동 결과의 연결만 되돌린다.
                if repair_pointer:
                    try:
                        with FileLock(str(folder / ".analysis.lock"), timeout=0):
                            if pointer_path.read_bytes() == pointer_bytes:
                                atomic_json(pointer_path, {"analysis": previous[0].parent.name})
                    except (OSError, Timeout):
                        pass
                return previous
        return selected
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def recover_notation_source(pdf: Path, paths: Paths, source_hash: str) -> Path | None:
    """Recover the selected timeline's exact XML, including verified older cache paths."""
    selected = load_last_timeline(pdf, paths, source_hash, repair_pointer=False)
    if selected is None:
        return None
    timeline_path, plan = selected
    if plan.data.get("analysis_fallback"):
        return None
    digest = plan.data.get("xml_sha256")
    if not isinstance(digest, str) or len(digest) != 64:
        return None
    folder = timeline_path.parent.parent
    candidates = []
    saved = plan.data.get("musicxml_path")
    if isinstance(saved, str) and saved:
        candidates.append(Path(saved))
    candidates.extend([
        folder / "recognition/reviewed.musicxml",
        folder / "recognition/score.mxl",
    ])
    signature = plan.data.get("signature", {})
    version = signature.get("omr") if isinstance(signature, dict) else None
    if type(version) is int and 1 <= version <= 100:
        candidates.append(folder / f"recognition/v{version}/score.mxl")
    for candidate in candidates:
        try:
            if (candidate.suffix.lower() in {".musicxml", ".mxl", ".xml"}
                    and candidate.is_file() and hash_file(candidate) == digest):
                return candidate.resolve()
        except OSError:
            continue
    return None
