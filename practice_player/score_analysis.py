# SPDX-License-Identifier: GPL-3.0-only
"""Offline-only OMR, navigation expansion and score/audio alignment.

Nothing in this module is imported by the playback position callback.
"""

import json
import os
import re
import shutil
import subprocess
import time
import zipfile
from collections import Counter
from hashlib import sha256
from pathlib import Path

import numpy as np
from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException
from filelock import FileLock

from .cache import hash_file
from .config import Paths
from .omr_runtime import executable as managed_executable
from .omr_runtime import managed_ready, managed_root, omr_environment
from .score import (
    AUTO_ANALYSIS_VERSION,
    SCORE_VERSION,
    ScoreTimeline,
    atomic_json,
    matching_musicxml,
    recover_notation_source,
)
from .score_geometry import LAYOUT_VERSION
from .score_input import OMR_VERSION, prepare_omr_input
from .score_sources import MUSICXML_LIMIT, embedded_musicxml, read_musicxml_bytes
from .score_symbols import PDF_SYMBOL_VERSION


class _RecognitionFailure(RuntimeError):
    def __init__(self, stage, reason):
        self.stage = stage
        super().__init__(str(reason))


class _RouteError(ValueError):
    pass


class _NavigationError(ValueError):
    pass


def _recognize(mode, stage, operation, *args, **kwargs):
    """Only recognition failures may select the geometry fallback."""
    try:
        return operation(*args, **kwargs)
    except Exception as error:
        if (mode != "auto" or stage == "navigation"
                or isinstance(error, (OSError, _RouteError, _NavigationError))):
            raise
        if isinstance(error, (ValueError, RuntimeError, ET.ParseError, DefusedXmlException,
                              zipfile.BadZipFile, KeyError, StopIteration)) or (
            type(error).__module__.startswith("music21.")
        ):
            raise _RecognitionFailure(stage, error) from error
        raise


def audiveris_path() -> str | None:
    candidates = [os.environ.get("AUDIVERIS_EXECUTABLE", "")]
    if managed_ready():
        candidates.append(str(managed_executable(managed_root())))
    candidates.append(shutil.which("audiveris"))
    if os.name == "nt":
        for folder in [os.environ.get("ProgramFiles", ""), os.environ.get("LOCALAPPDATA", "")]:
            candidates.extend([str(Path(folder) / "Audiveris" / "Audiveris.exe")])
    else:
        for folder in [
            Path.home() / ".vocal-separator-and-speed-adj/tools",
            Path.home() / "Applications",
            Path("/Applications"),
        ]:
            candidates.append(str(folder / "Audiveris.app/Contents/MacOS/Audiveris"))
    return next((str(Path(p).resolve()) for p in candidates if p and Path(p).is_file()), None)


def run_omr(pdf: Path, output: Path, report) -> Path:
    executable = audiveris_path()
    if not executable:
        raise RuntimeError(
            "Audiveris가 없습니다. install.command 또는 install.bat을 다시 더블클릭하세요. "
            "같은 이름의 MusicXML을 score_targets에 넣어도 됩니다."
        )
    output.mkdir(parents=True, exist_ok=True)
    prepared, metadata = prepare_omr_input(pdf, output / "input", report)
    atomic_json(output / "input.json", metadata)
    report("PDF의 음표·도돌이표·코다를 사전 인식 중…", None)
    command = [executable, "-batch", "-export", "-output", str(output), "--", str(prepared)]
    environment = omr_environment(executable)
    if Path(executable).resolve() == managed_executable(managed_root()).resolve():
        # Another Audiveris installation may have saved a different default language.
        command[1:1] = [
            "-constant", "org.audiveris.omr.text.Language.defaultSpecification=eng",
        ]
    # A distinct process group lets cancellation reap Java, not just the Python worker.
    with (output / "omr.log").open("wb") as log:
        process = subprocess.Popen(
            command,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=os.name != "nt",
            env=environment,
        )
        try:
            started, last_report, offset, stage = time.monotonic(), 0.0, 0, "인식 준비"
            stages = {
                "LOAD": "페이지 읽기", "BINARY": "이미지 정리", "SCALE": "보표 간격",
                "GRID": "보표", "HEADERS": "조표·박자표", "STEM_SEEDS": "음표 줄기",
                "BEAMS": "연결선", "LEDGERS": "덧줄", "HEADS": "음표 머리",
                "STEMS": "음표 길이", "REDUCTION": "음표 검증", "CUE_BEAMS": "작은 음표",
                "TEXTS": "문자·이동 지시", "MEASURES": "마디", "CHORDS": "화음",
                "CURVES": "붙임줄", "SYMBOLS": "연주 기호", "LINKS": "기호 연결",
                "RHYTHMS": "리듬", "PAGE": "페이지 마무리",
            }
            while True:
                try:
                    result = process.wait(timeout=1)
                    break
                except subprocess.TimeoutExpired:
                    elapsed = time.monotonic() - started
                    if elapsed >= 1800:
                        raise RuntimeError(
                            f"악보 인식 제한 시간(30분)을 초과했습니다. 로그: {output / 'omr.log'}"
                        )
                    with (output / "omr.log").open("rb") as stream:
                        stream.seek(offset)
                        chunk = stream.read()
                        offset = stream.tell()
                    matches = re.findall(rb"\[[^\]\r\n]*#(\d+)\].*StepMonitoring.*\| (\w+)", chunk)
                    if matches:
                        page, step = matches[-1]
                        label = stages.get(step.decode(), step.decode())
                        stage = f"{page.decode()}페이지 · {label}"
                    if elapsed - last_report >= 5:
                        report(f"악보 인식 중 · {stage} · {int(elapsed)}초 경과", None)
                        last_report = elapsed
        except BaseException:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                import signal

                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                if os.name != "nt":
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise
    candidates = sorted(output.rglob("*.mxl")) + sorted(output.rglob("*.musicxml"))
    log_text = (output / "omr.log").read_text(encoding="utf-8", errors="replace")
    if "Missing support for 'eng'" in log_text:
        raise RuntimeError(
            "영어 OCR 데이터가 없습니다. 설치 파일을 다시 더블클릭해 복구하세요. "
            "문자형 D.S./Coda를 누락한 결과는 사용하지 않습니다."
        )
    if result or len(candidates) != 1:
        raise RuntimeError(
            f"악보 인식 결과가 하나의 곡으로 생성되지 않았습니다. "
            f"Audiveris에서 인식 결과를 수정해 MusicXML로 저장하세요. 로그: {output / 'omr.log'}"
        )
    return candidates[0]


def read_musicxml(path: Path) -> bytes:
    if path.stat().st_size > MUSICXML_LIMIT:
        raise ValueError("MusicXML이 너무 큽니다 (최대 32 MB).")
    return read_musicxml_bytes(path.read_bytes())


def validate_omr_systems(xml, bars):
    """Equal totals cannot hide a missed bar in one system and an extra in another."""
    expected = Counter((b["page"], b["system"]) for b in bars.values())
    actual, page, system = Counter(), 0, 0
    for index, measure in enumerate(ET.fromstring(read_musicxml(xml)).findall("part/measure")):
        prints = measure.findall("print")
        if index and any(p.get("new-page") == "yes" for p in prints):
            page, system = page + 1, 0
        elif index and any(p.get("new-system") == "yes" for p in prints):
            system += 1
        actual[page, system] += 1
    if actual != expected:
        raise ValueError(
            "오선 인식 결과와 원본 PDF의 페이지/줄별 마디 수가 다릅니다. "
            "같은 판본의 GP/MusicXML을 선택하거나 인식 결과를 보정하세요."
        )


def parse_route_override(text: str, count: int) -> list[int]:
    route = []
    for item in text.split(","):
        match = re.fullmatch(r"\s*(\d+)(?:\s*-\s*(\d+))?\s*", item)
        if not match:
            raise _RouteError(
                "연주 순서는 1-8,1-6,9-16처럼 입력하세요 (인쇄 마디 번호 대신 원본 순번)."
            )
        first, last = int(match[1]), int(match[2] or match[1])
        if first < 1 or last < first or last > count or len(route) + last - first + 1 > 10000:
            raise _RouteError("연주 순서의 마디 범위가 올바르지 않습니다.")
        route.extend(range(first - 1, last))
    return route


def expand_navigation(measures, xml_measures) -> list[int]:
    """Expand barlines/voltas and one DC/DS jump, including open multi-system endings.

    music21.Expander can ignore volta exclusions when a DS is also present; keep
    the navigation explicit and bounded instead of silently adding an extra verse.
    """
    from music21 import bar, repeat

    pairs, stack, endings, active = {}, [], [], None
    commands, segnos, codas, fines = {}, [], [], set()
    for index, (measure, raw) in enumerate(zip(measures, xml_measures)):
        if isinstance(measure.leftBarline, bar.Repeat) and measure.leftBarline.direction == "start":
            stack.append(index)
        for node in raw.findall("barline/ending"):
            if node.get("type") == "start":
                if active is not None:
                    raise ValueError("겹치는 반복 엔딩을 확인하세요.")
                numbers = set()
                for token in node.get("number", "").split(","):
                    match = re.fullmatch(r"\s*(\d+)(?:\s*-\s*(\d+))?\s*", token)
                    if not match:
                        raise ValueError("반복 엔딩 번호를 인식하지 못했습니다.")
                    a, b = int(match[1]), int(match[2] or match[1])
                    if not 1 <= a <= b <= 8:
                        raise ValueError("반복 엔딩은 1~8회차만 지원합니다.")
                    numbers.update(range(a, b + 1))
                active = (index, numbers)
            elif active is not None:
                endings.append([active[0], index, active[1], node.get("type")])
                active = None
        right = measure.rightBarline
        if isinstance(right, bar.Repeat) and right.direction == "end":
            times = int(right.times or 2)
            if not 2 <= times <= 8:
                raise ValueError("반복 횟수는 2~8회만 지원합니다.")
            pairs[index] = (stack.pop() if stack else 0, times)
        for mark in measure.recurse().getElementsByClass(repeat.RepeatExpression):
            if isinstance(mark, repeat.Segno):
                segnos.append(index)
            elif isinstance(mark, repeat.Coda):
                codas.append(index)
            elif isinstance(mark, repeat.Fine):
                fines.add(index)
            elif isinstance(mark, repeat.RepeatExpressionCommand):
                commands[index] = type(mark).__name__
    if stack or active is not None:
        raise ValueError("닫히지 않은 도돌이표 또는 반복 엔딩이 있습니다.")
    if len(commands) > 1:
        raise ValueError("여러 D.C./D.S. 지시는 연주 순서를 직접 지정하세요.")
    brackets = []
    for first, last, numbers, closure in endings:
        enclosing = [end for end, (start, _) in pairs.items() if start <= first <= end]
        owner = min(enclosing) if enclosing else max((x for x in pairs if x < first), default=None)
        if owner is None:
            raise ValueError("반복 엔딩에 대응하는 도돌이표가 없습니다.")
        # An open first ending continues until its repeat barline, even over page breaks.
        if closure == "discontinue" and owner >= last:
            last = owner
        brackets.append((first, last, numbers, owner))
    route, passes = [], {end: 1 for end in pairs}
    jumped, index, command = False, 0, ""
    codas = sorted(set(codas))
    while index < len(measures):
        if len(route) >= 10000:
            raise ValueError("반복 이동이 끝나지 않습니다. 연주 순서를 확인하세요.")
        skipped = False
        if not jumped:
            for first, last, numbers, owner in brackets:
                if first == index and passes[owner] not in numbers:
                    index = last + 1
                    skipped = True
                    break
        if skipped:
            continue
        route.append(index)
        if jumped and "AlFine" in command and index in fines:
            break
        if jumped and "AlCoda" in command and codas and index == codas[0]:
            index = codas[1]
            continue
        if index in commands and not jumped:
            command = commands[index]
            if "AlCoda" in command and len(codas) != 2:
                raise ValueError("To Coda와 Coda 기호 한 쌍이 필요합니다.")
            if "AlFine" in command and not fines:
                raise ValueError("Fine 기호를 찾지 못했습니다.")
            if command.startswith("DalSegno"):
                if len(set(segnos)) != 1:
                    raise ValueError("D.S.에 대응하는 세뇨 기호 하나가 필요합니다.")
                target = segnos[0]
            elif command.startswith("DaCapo"):
                target = 0
            else:
                raise ValueError(f"지원하지 않는 이동 지시입니다: {command}")
            if target >= index:
                raise ValueError("되돌아갈 위치가 D.C./D.S. 앞에 있어야 합니다.")
            jumped, index = True, target
            continue
        if not jumped and index in pairs:
            first, times = pairs[index]
            if passes[index] < times:
                passes[index] += 1
                for inner in pairs:
                    if first <= pairs[inner][0] and inner < index:
                        passes[inner] = 1
                index = first
                continue
        index += 1
    return route


def parse_notation(
    xml_path: Path,
    page_count: int,
    route_override: str = "",
    *,
    document: bytes | None = None,
    measure_pages: dict | None = None,
) -> dict:
    from music21 import bar, converter, expressions, repeat, stream, tempo

    data = read_musicxml(xml_path) if document is None else document
    tree = ET.fromstring(data)
    # Remove namespace prefixes for interoperability with namespaced MusicXML.
    for node in tree.iter():
        node.tag = node.tag.split("}")[-1]
    if tree.tag != "score-partwise":
        raise ValueError("score-partwise MusicXML이 필요합니다.")
    score = converter.parseData(ET.tostring(tree, encoding="unicode"), format="musicxml")
    score.toSoundingPitch(inPlace=True)
    if not score.parts:
        raise ValueError("MusicXML에 연주 파트가 없습니다.")
    control = score.parts[0]
    measures = list(control.getElementsByClass(stream.Measure))
    xml_measures = tree.find("part").findall("measure")
    if not measures or len(measures) != len(xml_measures) or len(measures) > 2000:
        raise ValueError("악보 마디 구조를 읽지 못했거나 마디가 너무 많습니다.")
    warnings = []
    has_coda = any(x.get("tocoda") for x in tree.iter("sound"))
    has_fine = any(x.get("fine") for x in tree.iter("sound"))
    originals = []
    page = 0
    default_bpm = 120.0
    tempo_found = False
    for index, (measure, raw) in enumerate(zip(measures, xml_measures)):
        measure.editorial["source_index"] = index
        if measure_pages is not None:
            page = measure_pages[index]
        elif index and any(x.get("new-page") == "yes" for x in raw.findall("print")):
            page += 1
        if page >= page_count:
            raise ValueError("MusicXML의 페이지 수가 PDF보다 많습니다. 동일 판본을 사용하세요.")
        # music21 reads printed words/signs but ignores navigation in <sound>.
        existing = list(measure.recurse().getElementsByClass(repeat.RepeatExpression))
        for sound in raw.iter("sound"):
            additions = []
            if sound.get("segno") and not any(isinstance(x, repeat.Segno) for x in existing):
                additions.append((0, repeat.Segno()))
            if (sound.get("coda") or sound.get("tocoda")) and not any(
                isinstance(x, repeat.Coda) for x in existing
            ):
                additions.append((measure.highestTime if sound.get("tocoda") else 0, repeat.Coda()))
            if sound.get("fine") and not any(isinstance(x, repeat.Fine) for x in existing):
                additions.append((measure.highestTime, repeat.Fine()))
            if (sound.get("dacapo") == "yes" or sound.get("dalsegno")) and not any(
                isinstance(x, repeat.RepeatExpressionCommand) for x in existing
            ):
                stem = "DaCapo" if sound.get("dacapo") == "yes" else "DalSegno"
                suffix = "AlCoda" if has_coda else "AlFine" if has_fine else ""
                additions.append((measure.highestTime, getattr(repeat, stem + suffix)()))
            for offset, addition in additions:
                measure.insert(offset, addition)
                existing.append(addition)
        marks = [type(x).__name__ for x in existing]
        for side in (measure.leftBarline, measure.rightBarline):
            if isinstance(side, bar.Repeat):
                marks.append(f"repeat-{side.direction}")
        for text in measure.recurse().getElementsByClass(expressions.TextExpression):
            if re.search(r"(?:D\s*\.\s*[CS]|[Cc]oda|[Ss]egno|[Ff]ine)", str(text.content)):
                warnings.append(f"{index + 1}번 마디 이동 지시를 확인하세요: {text.content}")
        for mark in measure.recurse().getElementsByClass(tempo.MetronomeMark):
            bpm = mark.getQuarterBPM()
            if bpm and not tempo_found:
                default_bpm, tempo_found = float(bpm), True
        length = float(measure.duration.quarterLength)
        nominal = float(measure.barDuration.quarterLength)
        if length > nominal:
            warnings.append(
                f"{index + 1}번 마디의 인식 음표가 박자를 초과해 박자표 길이로 보정했습니다."
            )
            length = nominal
        if length <= 0:
            length = float(measure.barDuration.quarterLength)
        elif length < nominal and raw.get("implicit") != "yes":
            warnings.append(
                f"{index + 1}번 마디의 음표 길이가 부족해 박자표 길이를 유지했습니다. "
                "빠진 음표·쉼표를 확인하세요."
            )
            length = nominal
        originals.append(
            {
                "source_index": index,
                "measure": measure.measureNumberWithSuffix(),
                "page": page,
                "beats": length,
                "marks": marks,
            }
        )
    if page_count > 1 and page + 1 != page_count:
        warnings.append("MusicXML 페이지 구분이 PDF와 다릅니다. 시간표의 페이지를 수정하세요.")
    if route_override:
        route = parse_route_override(route_override, len(measures))
        warnings.append("사용자가 지정한 연주 순서를 사용합니다. 기호 자동 해석보다 우선합니다.")
    else:
        try:
            route = expand_navigation(measures, xml_measures)
        except Exception as error:
            raise _NavigationError(
                "반복/코다 구조가 불완전합니다. MusicXML을 수정하거나 "
                "연주 순서를 직접 입력하세요. " + str(error)
            ) from error
    if not route or len(route) > 10000:
        raise ValueError("연주 순서가 비어 있거나 반복 횟수가 지나치게 많습니다.")
    notes_by_bar = [[] for _ in measures]
    for part in score.parts:
        part_measures = list(part.getElementsByClass(stream.Measure))
        if len(part_measures) != len(measures):
            warnings.append("마디 수가 다른 파트는 음원 정렬에서 제외했습니다.")
            continue
        for index, measure in enumerate(part_measures):
            for note in measure.recurse().notes:
                start = float(note.getOffsetInHierarchy(measure))
                length = float(note.duration.quarterLength)
                if length > 0 and hasattr(note, "pitches"):
                    notes_by_bar[index].append((start, length, [p.midi % 12 for p in note.pitches]))
    beat = 0.0
    visits = Counter()
    cues, notes = [], []
    for source_index in route:
        original = originals[source_index]
        visits[source_index] += 1
        cues.append({**original, "beat": beat, "visit": visits[source_index]})
        for onset, duration, pitches in notes_by_bar[source_index]:
            notes.append((beat + onset, duration, pitches))
        beat += original["beats"]
    return {
        "cues": cues,
        "notes": notes,
        "beats": beat,
        "bpm": default_bpm,
        "warnings": sorted(set(warnings)),
        "originals": originals,
    }


def align_audio(audio: Path, notation: dict, report) -> tuple[list[float], float, dict]:
    import librosa

    report("음원과 악보의 마디 위치를 사전 정렬 중…", None)
    # FFmpeg accepts the same broad input formats as the separation pipeline.
    import tempfile

    import soundfile as sf

    from .audio import run_ffmpeg

    with tempfile.TemporaryDirectory(prefix="practice-align-") as scratch:
        decoded = Path(scratch) / "mono.wav"
        run_ffmpeg(
            [
                "-i",
                str(audio),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "11025",
                "-t",
                "1801",
                "-c:a",
                "pcm_f32le",
                str(decoded),
            ]
        )
        samples, rate = sf.read(decoded, dtype="float32")
    duration = len(samples) / rate
    if duration > 1800 or duration < 1:
        raise ValueError("자동 정렬은 1초~30분 음원을 지원합니다.")
    hop = max(512, int(np.ceil(len(samples) / 3000)))
    chroma = librosa.feature.chroma_stft(y=samples, sr=rate, n_fft=4096, hop_length=hop)
    energy = librosa.feature.rms(y=samples, frame_length=4096, hop_length=hop)[0]
    active = np.flatnonzero(energy > max(1e-6, float(np.max(energy)) * 0.015))
    if not len(active):
        raise ValueError("음원에서 정렬할 소리를 찾지 못했습니다.")
    first_frame, last_frame = int(active[0]), int(active[-1]) + 1
    chroma = chroma[:, first_frame:last_frame]
    energy = energy[first_frame:last_frame]
    # Restrict matrix size and relative slope; never stretch a single bar over the whole song.
    count = min(3000, max(32, chroma.shape[1]))
    grid = np.linspace(0, notation["beats"], count, endpoint=False)
    reference = np.full((12, count), 0.01, dtype=np.float32)
    for onset, length, pitches in notation["notes"]:
        first = np.searchsorted(grid, onset)
        last = max(first + 1, np.searchsorted(grid, onset + length))
        for pitch in pitches:
            reference[int(pitch), first:last] += 1
    if not np.any(reference > 0.01):
        raise ValueError("음높이 정보를 인식하지 못했습니다. MusicXML 또는 BPM 모드를 사용하세요.")
    chroma += 0.01
    from scipy.spatial.distance import cdist

    cost = cdist(reference.T, chroma.T, metric="cosine")
    cost[:, energy < max(1e-6, float(np.max(energy)) * 0.015)] += 1.0
    _, path = librosa.sequence.dtw(
        C=cost,
        subseq=False,
        step_sizes_sigma=np.array([[1, 1], [1, 2], [2, 1]]),
        weights_add=np.array([0, 0.03, 0.03]),
        weights_mul=np.array([1, 2, 2]),
    )
    path = path[::-1]
    mapping = np.full(count, np.nan)
    for i in np.unique(path[:, 0]):
        mapping[i] = (first_frame + np.median(path[path[:, 0] == i, 1])) * hop / rate
    valid = np.isfinite(mapping)
    mapping = np.interp(np.arange(count), np.where(valid)[0], mapping[valid])
    starts = np.interp([cue["beat"] for cue in notation["cues"]], grid, mapping)
    # Reject collapsed/ambiguous bars rather than publish impossible playback order.
    if np.any(np.diff(starts) <= 0.02):
        raise ValueError(
            "자동 정렬에서 여러 마디가 같은 시간에 겹쳤습니다. 연주 순서 또는 인식을 수정하세요."
        )
    end = min(duration, float(mapping[-1] + hop / rate))
    similarity = float(1 - np.mean(cost[path[:, 0], path[:, 1]]))
    return (
        starts.tolist(),
        max(end, float(starts[-1]) + 0.01),
        {
            "method": "chroma-dtw",
            "similarity": round(similarity, 4),
            "audio_duration": duration,
        },
    )


def _analyze_pdf_timing(
    audio, pdf, folder, audio_hash, pdf_hash, page_count, report,
    *, bpm, lead_in, route_override, meter_beats, meter_unit, force,
    automatic=None, cache_only=False, validate_source=lambda: None, publish=True,
):
    """Use visible bars and an explicit meter, without OMR or note alignment."""
    from .score_geometry import detect_pdf_layout

    if (type(meter_beats) is not int or not 1 <= meter_beats <= 32
            or type(meter_unit) is not int or meter_unit not in (1, 2, 4, 8, 16, 32)):
        raise ValueError("PDF 마디 박자는 분자 1~32, 분모 1·2·4·8·16·32로 입력하세요.")
    signature = {
        "version": SCORE_VERSION, "mode": "pdf", "bpm": bpm, "lead_in": lead_in,
        "meter_beats": meter_beats, "meter_unit": meter_unit, "route": route_override,
        "pdf_recognition": LAYOUT_VERSION, "timing": "pdf-bars-fixed-tempo-v1",
    }
    if automatic is not None:
        signature.update(mode="auto", automatic=automatic["request"])
    key = sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()[:20]
    destination = folder / key
    if not force and (destination / "timeline.json").is_file():
        try:
            plan = ScoreTimeline.load(destination / "timeline.json")
            if (plan.data["audio_sha256"] == audio_hash
                    and plan.data["pdf_sha256"] == pdf_hash
                    and plan.data.get("signature") == signature):
                cached = destination / "timeline.json"
            else:
                cached = None
        except (ValueError, KeyError, OSError, TypeError):
            cached = None
        if cached:
            if hash_file(audio) != audio_hash or hash_file(pdf) != pdf_hash:
                raise ValueError("분석 중 원본 파일이 변경되었습니다. 다시 분석하세요.")
            validate_source()
            if publish:
                atomic_json(folder / "latest.json", {"analysis": key})
            report("저장된 PDF 마디 시간표를 불러왔습니다.", 1.0)
            return cached
    if cache_only:
        return None
    _, bars, layout = detect_pdf_layout(pdf, None, report)
    if not bars or set(bars) != set(range(len(bars))) or len(bars) > 2000:
        raise ValueError("PDF에서 순서대로 연결할 수 있는 1~2,000마디가 필요합니다.")
    if automatic is not None and not route_override:
        from .score_symbols import locate_mark, text_marks

        if any(locate_mark(mark, bars) is not None for mark in text_marks(pdf)) or any(
            bar.get("repeat_start") or bar.get("repeat_end") for bar in bars.values()
        ):
            raise _NavigationError(
                "PDF에 반복·이동 기호가 있어 인쇄 순서로 대체할 수 없습니다. "
                "인식 결과를 보정한 MusicXML을 사용하거나 연주 순서를 직접 입력하세요."
            )
    route = (parse_route_override(route_override, len(bars))
             if route_override else list(range(len(bars))))
    # BPM은 4분음표 기준이므로 6/8 한 마디는 3박으로 계산한다.
    quarter_beats = meter_beats * 4 / meter_unit
    originals = [
        {"source_index": index, "measure": str(index + 1), "page": bars[index]["page"],
         "beats": quarter_beats, "marks": []}
        for index in range(len(bars))
    ]
    visits, cues = Counter(), []
    for position, index in enumerate(route):
        visits[index] += 1
        beat = position * quarter_beats
        cues.append({**originals[index], "visit": visits[index], "beat": beat,
                     "start": round(lead_in + beat * 60 / bpm, 4)})
    end = lead_in + len(route) * quarter_beats * 60 / bpm
    warnings = list(layout.get("warnings", [])) + [
        "PDF 마디선과 입력한 BPM·박자로 계산한 시간 추정입니다. "
        "음원과 맞는지 시간표에서 확인하세요.",
        "못갖춘마디·박자/템포 변화·복합 쉼표는 자동 해석하지 않습니다. "
        "해당 구간의 시간을 보정하세요.",
        ("사용자가 입력한 연주 순서를 사용합니다. 반복·D.C./D.S.·코다를 포함하는지 확인하세요."
         if route_override else
         "PDF의 마디를 인쇄 순서대로 한 번씩 진행합니다. "
         "반복·D.C./D.S.·코다는 연주 순서에 직접 입력하세요."),
    ]
    fallback = automatic.get("fallback") if automatic is not None else None
    if fallback:
        warnings.insert(0, "음표·반복 인식을 완료하지 못해 PDF 마디선과 입력 BPM·박자로 "
                        "시간을 추정했습니다. 사유: " + fallback["reason"])
    if hash_file(audio) != audio_hash or hash_file(pdf) != pdf_hash:
        raise ValueError("분석 중 원본 파일이 변경되었습니다. 다시 분석하세요.")
    validate_source()
    plan = ScoreTimeline({
        "version": SCORE_VERSION, "audio_sha256": audio_hash, "pdf_sha256": pdf_hash,
        "pdf_name": pdf.name, "page_count": page_count,
        "notation_source": {"method": "pdf-geometry", "version": 1},
        "signature": signature, "cues": cues, "end": end,
        "alignment": {"method": "pdf-fixed-tempo", "bpm": bpm, "lead_in": lead_in,
                      "meter_beats": meter_beats, "meter_unit": meter_unit},
        "warnings": warnings, "originals": originals, "pdf_layout": layout,
        "reviewed": False, "offset": 0.0,
        **({"analysis_fallback": fallback} if fallback else {}),
    })
    if destination.exists() and force:
        key += f"-{time.time_ns()}"
        destination = folder / key
    destination.mkdir(exist_ok=True)
    plan.save(destination / "timeline.json")
    if publish:
        atomic_json(folder / "latest.json", {"analysis": key})
    report("PDF 마디 시간표 준비 완료 · 반주와 시간을 확인하세요.", 1.0)
    return destination / "timeline.json"


def analyze_score(
    audio: Path,
    pdf: Path,
    paths: Paths,
    report=lambda *_: None,
    *,
    musicxml: Path | None = None,
    gp_track=1,
    gp_display=None,
    mode="auto",
    bpm=120.0,
    meter_beats=4,
    meter_unit=4,
    lead_in=0.0,
    route_override="",
    force=False,
    publish=True,
) -> Path:
    from pypdf import PdfReader

    audio, pdf = audio.resolve(strict=True), pdf.resolve(strict=True)
    reader = PdfReader(pdf)
    if reader.is_encrypted or not 1 <= len(reader.pages) <= 200:
        raise ValueError("암호가 없는 1~200페이지 PDF가 필요합니다.")
    audio_hash, pdf_hash = hash_file(audio), hash_file(pdf)
    if (mode not in {"auto", "audio", "tempo", "pdf"}
            or not 20 <= bpm <= 400 or not 0 <= lead_in <= 1800):
        raise ValueError("정렬 설정이 올바르지 않습니다.")
    if mode in {"auto", "pdf"} and (
        type(meter_beats) is not int or not 1 <= meter_beats <= 32
        or type(meter_unit) is not int or meter_unit not in (1, 2, 4, 8, 16, 32)
    ):
        raise ValueError("마디 박자는 분자 1~32, 분모 1·2·4·8·16·32로 입력하세요.")
    if route_override:
        parse_route_override(route_override, 2000)
    if mode == "pdf" and (musicxml is not None or gp_display is not None):
        raise ValueError(
            "PDF 마디선 모드에서는 PDF만 선택하세요. MusicXML/GP 정렬은 다른 모드를 사용하세요."
        )
    if musicxml is not None:
        musicxml = musicxml.resolve(strict=True)
    folder = paths.processed / audio_hash / "scores" / pdf_hash
    folder.mkdir(parents=True, exist_ok=True)
    with FileLock(str(folder / ".analysis.lock"), timeout=3600):
        pdf_options = dict(bpm=bpm, lead_in=lead_in, route_override=route_override,
                           meter_beats=meter_beats, meter_unit=meter_unit, force=force,
                           publish=publish)
        if mode == "pdf":
            return _analyze_pdf_timing(
                audio, pdf, folder, audio_hash, pdf_hash, len(reader.pages), report, **pdf_options,
            )
        from .guitar_pro import GP_EXTENSIONS

        selection = "explicit" if musicxml is not None else None
        if musicxml is None:
            musicxml = matching_musicxml(pdf)
            if musicxml is not None:
                selection = "matching"
        if musicxml is None:
            reviewed = folder / "recognition/reviewed.musicxml"
            if reviewed.is_file():
                musicxml, selection = reviewed, "reviewed"
        if musicxml is None:
            musicxml = recover_notation_source(pdf, paths, audio_hash)
            if musicxml is not None:
                selection = "recovered"
        if musicxml is None:
            musicxml = embedded_musicxml(reader, folder)
            if musicxml is not None:
                selection = "embedded"
        if musicxml is None:
            candidate = folder / f"recognition/v{OMR_VERSION}/score.mxl"
            if candidate.is_file():
                musicxml = candidate
            selection = "omr"
        allow_fallback = selection == "omr"
        labels = {
            "explicit": "선택한 MusicXML/GP 원본", "matching": "같은 이름의 MusicXML/GP 원본",
            "recovered": "이전 분석과 내용이 일치하는 인식 파일", "reviewed": "보정한 MusicXML",
            "embedded": "PDF에 내장된 MusicXML", "omr": "PDF 음표 인식",
        }
        report(f"악보 입력 · {labels[selection]}", None)
        source_hash = hash_file(musicxml) if musicxml is not None else None
        if mode == "auto" and (gp_display or (
            musicxml is not None and musicxml.suffix.lower() in GP_EXTENSIONS
        )):
            mode = "audio"
        automatic = None
        if mode == "auto" and allow_fallback:
            automatic = {"request": {"version": AUTO_ANALYSIS_VERSION,
                                      "notation_sha256": source_hash, "omr": OMR_VERSION}}

        def unchanged():
            if (hash_file(audio) != audio_hash or hash_file(pdf) != pdf_hash
                    or (musicxml is not None and hash_file(musicxml) != source_hash)):
                raise ValueError("분석 중 원본 파일이 변경되었습니다. 다시 분석하세요.")

        def record_generated(path, expected_hash):
            nonlocal musicxml, source_hash
            if hash_file(path) != expected_hash:
                raise ValueError("인식 중 MusicXML 파일이 변경되었습니다. 다시 분석하세요.")
            musicxml, source_hash = path, expected_hash
            if automatic is not None:
                automatic["request"]["notation_sha256"] = expected_hash
            unchanged()

        if automatic is not None:
            cached = _analyze_pdf_timing(
                audio, pdf, folder, audio_hash, pdf_hash, len(reader.pages), report,
                automatic=automatic, cache_only=True, validate_source=unchanged, **pdf_options,
            )
            if cached:
                unchanged()
                return cached
        try:
            return _analyze_notation(
                audio, pdf, folder, audio_hash, pdf_hash, reader, report,
                musicxml=musicxml, gp_track=gp_track, gp_display=gp_display, mode=mode,
                bpm=bpm, lead_in=lead_in, route_override=route_override, force=force,
                meter_beats=meter_beats, meter_unit=meter_unit, validate_source=unchanged,
                record_generated=record_generated, allow_fallback=allow_fallback,
                selection=selection, publish=publish,
            )
        except _RecognitionFailure as error:
            if automatic is None:
                raise
            unchanged()
            report("음표·반복 인식을 마치지 못해 마디선과 BPM으로 시간을 계산합니다.", None)
            automatic["fallback"] = {
                "from": "notation", "to": "pdf-geometry", "stage": error.stage,
                "reason": str(error),
            }
            result = _analyze_pdf_timing(
                audio, pdf, folder, audio_hash, pdf_hash, len(reader.pages), report,
                automatic=automatic, validate_source=unchanged, **pdf_options,
            )
            unchanged()
            return result


def _analyze_notation(
    audio, pdf, folder, audio_hash, pdf_hash, reader, report, *, musicxml, gp_track,
    gp_display, mode, bpm, lead_in, route_override, force, meter_beats, meter_unit,
    validate_source, record_generated, allow_fallback, selection, publish,
):
    from .guitar_pro import GP_EXTENSIONS, GP_VERSION, read_gp

    omr_folder = folder / "recognition"
    # 이미 있는 악보 원본은 인식 실패를 이유로 마디선 시간표로 바꾸지 않는다.
    recognition_mode = mode if allow_fallback else "audio"
    source_path, source_hash, source_info = None, None, {}
    if musicxml is None:
        work = omr_folder / f"v{OMR_VERSION}" / f"work-{time.time_ns()}"
        generated = _recognize(recognition_mode, "omr", run_omr, pdf, work, report)
        generated_hash = hash_file(generated)
        if hash_file(pdf) != pdf_hash:
            raise ValueError("인식 중 PDF가 변경되었습니다. 다시 분석하세요.")
        musicxml = omr_folder / f"v{OMR_VERSION}" / "score.mxl"
        shutil.copyfile(generated, musicxml)
        # 첫 fallback도 다음 요청과 같은 인식 파일 해시를 사용해야 한다.
        record_generated(musicxml, generated_hash)
        atomic_json(
            musicxml.with_suffix(".json"),
            json.loads((work / "input.json").read_text(encoding="utf-8")),
        )
    if musicxml.suffix.lower() in GP_EXTENSIONS:
        source_path = musicxml.resolve(strict=True)
        source_hash = hash_file(source_path)
        report("Guitar Pro의 음표·박자·반복을 직접 읽는 중…", None)
        document, source_info = read_gp(source_path, gp_track)
        if hash_file(source_path) != source_hash:
            raise ValueError("분석 중 Guitar Pro 파일이 변경되었습니다.")
        converted = folder / "notation"
        converted.mkdir(exist_ok=True)
        musicxml = converted / f"{source_hash}-track{gp_track}-v{GP_VERSION}.musicxml"
        # Generated intermediate only; never rewrite the supplied score.
        temporary = musicxml.with_suffix(".tmp")
        temporary.write_bytes(document)
        temporary.replace(musicxml)
    elif musicxml == omr_folder / f"v{OMR_VERSION}" / "score.mxl":
        source_info = json.loads(musicxml.with_suffix(".json").read_text(encoding="utf-8"))
    xml_hash = hash_file(musicxml)
    signature = {
        "version": SCORE_VERSION,
        "xml": xml_hash,
        "mode": mode,
        "bpm": bpm,
        "lead_in": lead_in,
        "route": route_override,
        "music21": "10.5.2",
        "alignment": "chroma-dtw-v3-full-score",
        "pdf_recognition": LAYOUT_VERSION,
        "pdf_symbols": PDF_SYMBOL_VERSION,
        "notation_policy": "omr" if allow_fallback and mode == "auto" else "preserve-source",
        "omr": OMR_VERSION,
        "source_sha256": source_hash,
        "source_import": source_info,
        "gp_display": gp_display["signature"] if gp_display else None,
    }
    if mode == "auto":
        signature.update(meter_beats=meter_beats, meter_unit=meter_unit,
                         automatic=AUTO_ANALYSIS_VERSION)
    key = sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()[:20]
    destination = folder / key
    if not force and (destination / "timeline.json").exists():
        try:
            plan = ScoreTimeline.load(destination / "timeline.json")
            if plan.data["audio_sha256"] == audio_hash and plan.data["pdf_sha256"] == pdf_hash:
                cached = destination / "timeline.json"
            else:
                cached = None
        except (ValueError, KeyError, OSError):
            cached = None
        if cached:
            validate_source()
            if publish:
                atomic_json(folder / "latest.json", {"analysis": key})
            report("저장된 악보 사전 분석을 불러왔습니다.", 1.0)
            return cached
    report("도돌이표·반복 엔딩·D.C./D.S.·코다 연주 순서를 계산 중…", None)
    from .score_layout import analyze_layout
    from .score_symbols import printed_measure_numbers, supplement_xml

    layout, pages, document, additions, extra_warnings = {}, None, None, [], []
    layout_error = None
    try:
        if gp_display:
            from .gp_render import native_layout

            _, bars, layout = native_layout(gp_display)
        else:
            _, bars, layout = _recognize(
                recognition_mode, "layout", analyze_layout, pdf, musicxml, report,
            )
    except (ValueError, _RecognitionFailure) as failure:
        error = failure.__cause__ if isinstance(failure, _RecognitionFailure) else failure
        if not isinstance(error, ValueError):
            raise
        if source_info.get("method") in {"guitar-pro", "standard-staves-from-mixed-pdf"}:
            if recognition_mode == "auto":
                raise _RecognitionFailure("layout", error) from error
            raise ValueError(
                "원본 악보와 PDF의 마디를 정확히 연결하지 못했습니다. "
                "같은 판본·트랙인지 확인하세요. " + str(error)
            ) from error
        layout_error = error
        extra_warnings.append("PDF 배치를 자동 연결하지 못했습니다: " + str(error))
    else:
        if source_info.get("method") == "standard-staves-from-mixed-pdf":
            _recognize(recognition_mode, "layout", validate_omr_systems, musicxml, bars)
        pages = {i: bar["page"] for i, bar in bars.items()}
        extra_warnings.extend(layout.get("warnings", []))
        # Geometric evidence is required before adding a missing navigation sign.
        if layout["method"] == "pdf-staves-barlines" and not route_override:
            document, additions, notes = _recognize(
                recognition_mode, "navigation", supplement_xml, pdf, musicxml, bars,
            )
            extra_warnings.extend(notes)
    notation = _recognize(
        recognition_mode, "notation", parse_notation, musicxml, len(reader.pages), route_override,
        document=document, measure_pages=pages,
    )
    if layout_error is not None and recognition_mode == "auto":
        # 화면 배치 실패 때문에 이미 읽은 반복 순서까지 버리면 안 된다.
        if not route_override and not any(cue["marks"] for cue in notation["originals"]):
            raise _RecognitionFailure("layout", layout_error) from layout_error
        extra_warnings.append(
            "화면 배치는 연결하지 못했지만 인식한 연주 순서와 MusicXML 페이지를 유지합니다."
        )
    notation["warnings"].extend(extra_warnings)
    notation["warnings"].extend(source_info.get("warnings", []))
    if source_info and layout.get("method") == "pdf-staves-barlines":
        labels = printed_measure_numbers(pdf, bars)
        changed = sum(label != notation["originals"][i]["measure"]
                      for i, label in labels.items())
        for cue in notation["originals"] + notation["cues"]:
            if cue["source_index"] in labels:
                cue["measure"] = labels[cue["source_index"]]
        layout["printed_measure_numbers"] = len(labels)
        if changed:
            notation["warnings"].append(
                f"인식 원본과 다른 {changed}개 마디 번호를 PDF 인쇄 번호로 표시합니다. "
                "연주 순서는 바꾸지 않았습니다."
            )
    if source_info.get("method") == "standard-staves-from-mixed-pdf":
        notation["warnings"].append(
            "오선 음표를 인식하고 타브는 원본 표시·마디 위치 확인에 사용했습니다. "
            "타브 프렛 숫자의 음높이를 인식한 결과는 아닙니다."
        )
    text = " ".join(page.extract_text() or "" for page in reader.pages)
    marks = {mark for cue in notation["originals"] for mark in cue["marks"]}
    # Vector PDFs can reveal navigation words that OCR missed. Fail visibly;
    # guessing a jump here could send the reader to an unrelated page.
    if (
        not route_override
        and re.search(r"D\s*\.\s*S", text, re.I)
        and not any(mark.startswith("DalSegno") for mark in marks)
    ):
        message = (
            "PDF에는 D.S.가 있지만 인식 결과에 없습니다. Audiveris/MusicXML에서 "
            "세뇨·코다를 보정하거나 연주 순서를 직접 입력하세요."
        )
        raise _NavigationError(message)
    if (
        not route_override
        and re.search(r"D\s*\.\s*C", text, re.I)
        and not any(mark.startswith("DaCapo") for mark in marks)
    ):
        message = "PDF의 D.C.를 인식하지 못했습니다. MusicXML 또는 연주 순서를 보정하세요."
        raise _NavigationError(message)
    if mode in {"auto", "audio"}:
        starts, end, alignment = align_audio(audio, notation, report)
    else:
        starts = [lead_in + cue["beat"] * 60 / bpm for cue in notation["cues"]]
        end = lead_in + notation["beats"] * 60 / bpm
        alignment = {"method": "fixed-tempo", "bpm": bpm, "lead_in": lead_in}
        notation["warnings"].append(
            "BPM 기반 추정입니다. 실제 연주 템포·페르마타는 시간표에서 보정하세요."
        )
    cues = [
        {**cue, "start": round(float(start), 4)} for cue, start in zip(notation["cues"], starts)
    ]
    if (
        hash_file(audio) != audio_hash
        or hash_file(pdf) != pdf_hash
        or hash_file(musicxml) != xml_hash
        or (source_path is not None and hash_file(source_path) != source_hash)
    ):
        raise ValueError("분석 중 원본 파일이 변경되었습니다. 다시 분석하세요.")
    validate_source()
    plan = ScoreTimeline(
        {
            "version": SCORE_VERSION,
            "audio_sha256": audio_hash,
            "pdf_sha256": pdf_hash,
            "pdf_name": pdf.name,
            "page_count": len(reader.pages),
            "xml_sha256": hash_file(musicxml),
            "musicxml_path": str(musicxml),
            "notation_selection": selection,
            "notation_source": {
                "path": str(source_path) if source_path else None,
                "sha256": source_hash, **source_info,
            },
            "signature": signature,
            "cues": cues,
            "end": end,
            "alignment": alignment,
            "warnings": notation["warnings"],
            "originals": notation["originals"],
            "pdf_layout": layout,
            "gp_display": gp_display,
            "pdf_symbols_added": additions,
            "reviewed": False,
            "offset": 0.0,
        }
    )
    # Do not overwrite an edited timeline when explicitly re-running analysis.
    if destination.exists() and force:
        key += f"-{time.time_ns()}"
        destination = folder / key
    destination.mkdir(exist_ok=True)
    plan.save(destination / "timeline.json")
    if publish:
        atomic_json(folder / "latest.json", {"analysis": key})
    report("악보 분석 완료 · 시간표와 반복 이동을 확인하세요.", 1.0)
    return destination / "timeline.json"
