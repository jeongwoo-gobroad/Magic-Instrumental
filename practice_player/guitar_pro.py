# SPDX-License-Identifier: GPL-3.0-only
"""Read GP3/4/5 notation without OMR, for pairing with the same printed score."""

import math
from fractions import Fraction
from xml.etree.ElementTree import Element, SubElement, tostring

GP_VERSION = 1
GP_EXTENSIONS = {".gp3", ".gp4", ".gp5"}


def node(parent, tag, value=None, **attributes):
    result = SubElement(parent, tag, attributes)
    if value is not None:
        result.text = str(value)
    return result


def barline(measure, side):
    existing = measure.find(f"barline[@location='{side}']")
    return existing if existing is not None else node(measure, "barline", location=side)


def navigation(measure, header):
    signs = {
        "Segno": ("segno", "gp-segno", ""), "Coda": ("coda", "gp-coda", ""),
        "Fine": ("fine", "yes", "Fine"), "Da Coda": ("tocoda", "gp-coda", ""),
        "Da Capo": ("dacapo", "yes", "D.C."),
        "Da Capo al Fine": ("dacapo", "yes", "D.C. al Fine"),
        "Da Capo al Coda": ("dacapo", "yes", "D.C. al Coda"),
        "Da Segno": ("dalsegno", "gp-segno", "D.S."),
        "Da Segno al Fine": ("dalsegno", "gp-segno", "D.S. al Fine"),
        "Da Segno al Coda": ("dalsegno", "gp-segno", "D.S. al Coda"),
    }
    for sign in (header.direction, header.fromDirection):
        if sign is None:
            continue
        if sign.name not in signs:
            raise ValueError(f"지원하지 않는 Guitar Pro 이동 기호입니다: {sign.name}")
        key, value, words = signs[sign.name]
        direction = node(measure, "direction")
        node(node(direction, "direction-type"), "words", words)
        node(direction, "sound", **{key: value})


def read_gp(path, track_number=1):
    import guitarpro

    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("Guitar Pro 파일은 최대 16 MB까지 지원합니다.")
    try:
        song = guitarpro.parse(path)
    except Exception as error:
        raise ValueError("GP3/GP4/GP5 파일을 읽지 못했습니다. 파일 형식을 확인하세요.") from error
    if not 1 <= len(song.measureHeaders) <= 2000 or not 1 <= len(song.tracks) <= 64:
        raise ValueError("Guitar Pro는 최대 2,000마디·64트랙까지 지원합니다.")
    if not 1 <= track_number <= len(song.tracks):
        raise ValueError(f"GP 트랙 번호를 1~{len(song.tracks)}에서 선택하세요.")
    track = song.tracks[track_number - 1]
    if track.isPercussionTrack:
        raise ValueError("GP 음원 정렬에는 음높이가 있는 트랙을 선택하세요.")
    if len(track.measures) != len(song.measureHeaders) or not 1 <= len(track.strings) <= 12:
        raise ValueError("Guitar Pro 마디 또는 현 구성이 올바르지 않습니다.")
    # Retain exact tuplet timing rather than rounding GP ticks to milliseconds.
    scale, count = 1, 0
    for measure in track.measures:
        for voice in measure.voices:
            for beat in voice.beats:
                count += max(1, len(beat.notes))
                scale = math.lcm(scale, Fraction(beat.duration.time).denominator)
    if count > 200000 or scale > 4096:
        raise ValueError("Guitar Pro 음표 수 또는 잇단음표가 너무 복잡합니다.")
    root = Element("score-partwise", version="4.0")
    listed = node(node(root, "part-list"), "score-part", id="P1")
    node(listed, "part-name", track.name or "Guitar")
    part = node(root, "part", id="P1")
    previous_notes = {}
    active_ending = None
    warnings = set()
    for index, source in enumerate(track.measures):
        header = source.header
        length = int(header.length * scale)
        if length <= 0 or length > 960 * scale * 64:
            raise ValueError("Guitar Pro 마디 길이가 범위를 벗어났습니다.")
        measure = node(part, "measure", number=str(index + 1))
        attrs = node(measure, "attributes")
        node(attrs, "divisions", 960 * scale)
        meter = node(attrs, "time")
        node(meter, "beats", header.timeSignature.numerator)
        node(meter, "beat-type", header.timeSignature.denominator.value)
        if index == 0:
            clef = node(attrs, "clef")
            node(clef, "sign", "G")
            node(clef, "line", 2)
            node(node(measure, "direction"), "sound", tempo=str(song.tempo))
        if header.tripletFeel.name != "none":
            warnings.add("GP 셔플/스윙 미세 타이밍은 음원 정렬과 시간표에서 확인하세요.")
        navigation(measure, header)
        if header.isRepeatOpen:
            node(barline(measure, "left"), "repeat", direction="forward")
        mask = header.repeatAlternative
        if mask < 0 or mask > 255:
            raise ValueError("Guitar Pro 반복 엔딩 번호를 확인하세요.")
        ending = ",".join(str(n + 1) for n in range(8) if mask & (1 << n))
        if ending and ending != active_ending:
            node(barline(measure, "left"), "ending", number=ending, type="start")
        active_ending = ending or None
        voices = [(i, v) for i, v in enumerate(source.voices)
                  if any(b.status.name != "empty" for b in v.beats)]
        if not voices:
            rest = node(measure, "note")
            node(rest, "rest", measure="yes")
            node(rest, "duration", length)
        for sequence_index, (voice_index, voice) in enumerate(voices):
            if sequence_index:
                node(node(measure, "backup"), "duration", length)
            cursor = 0
            for beat in voice.beats:
                if beat.status.name == "empty":
                    continue
                onset = int(Fraction(beat.start - header.start) * scale)
                duration = int(Fraction(beat.duration.time) * scale)
                if onset < cursor or duration <= 0 or onset + duration > length:
                    raise ValueError(f"GP {index + 1}마디의 음표 길이가 박자와 맞지 않습니다.")
                if onset > cursor:
                    node(node(measure, "forward"), "duration", onset - cursor)
                pitched = [n for n in beat.notes if n.type.name in {"normal", "tie"}]
                for chord_index, note in enumerate(pitched or [None]):
                    target = node(measure, "note")
                    if chord_index:
                        node(target, "chord")
                    if note is None:
                        node(target, "rest")
                    else:
                        midi = note.realValue + track.offset
                        if not 0 <= midi <= 127:
                            raise ValueError("Guitar Pro 음높이가 MIDI 범위를 벗어났습니다.")
                        names = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
                        name = names[midi % 12]
                        pitch = node(target, "pitch")
                        node(pitch, "step", name[0])
                        if len(name) == 2:
                            node(pitch, "alter", 1)
                        node(pitch, "octave", midi // 12 - 1)
                        key = (voice_index, note.string)
                        if note.type.name == "tie":
                            if key not in previous_notes:
                                raise ValueError("Guitar Pro 붙임줄 앞 음표가 없습니다.")
                            node(previous_notes[key], "tie", type="start")
                            node(target, "tie", type="stop")
                        previous_notes[key] = target
                    node(target, "duration", duration)
                    node(target, "voice", voice_index + 1)
                cursor = onset + duration
            if cursor < length:
                rest = node(measure, "note")
                node(rest, "rest")
                node(rest, "duration", length - cursor)
                node(rest, "voice", voice_index + 1)
        next_mask = (
            song.measureHeaders[index + 1].repeatAlternative
            if index + 1 < len(track.measures) else 0
        )
        if ending and (next_mask != mask or header.repeatClose >= 0):
            node(barline(measure, "right"), "ending", number=ending, type="stop")
            active_ending = None
        if header.repeatClose >= 0:
            if not 1 <= header.repeatClose <= 7:
                raise ValueError("GP 반복 횟수는 총 2~8회까지 지원합니다.")
            node(barline(measure, "right"), "repeat",
                 direction="backward", times=str(header.repeatClose + 1))
    warnings.add("GP 음표·박자·반복을 직접 읽었습니다. 벤딩 등 연주 효과와 음원 일치를 확인하세요.")
    return tostring(root, encoding="utf-8", xml_declaration=True), {
        "method": "guitar-pro", "version": GP_VERSION, "track": track_number,
        "track_name": track.name, "measures": len(track.measures), "warnings": sorted(warnings),
    }
