# SPDX-License-Identifier: GPL-3.0-only
"""Portable, stored ZIP: original score, rendered pages, audio and reviewed timing."""

import io
import json
import math
import re
import tempfile
import zipfile
from pathlib import Path

import numpy as np
import soundfile as sf

from .audio import inspect_audio, publish_export, run_ffmpeg
from .cache import hash_file
from .config import ROOT, Paths
from .gp_collection import portable_timing, prepare_collection
from .score import ScoreTimeline

MAGIC_VERSION = 4
MAX_AUDIO_BYTES = 512 * 1024 * 1024
MAX_BUNDLE_BYTES = 1024 * 1024 * 1024
FLOAT_FORMAT = "aformat=sample_fmts=flt:sample_rates=44100:channel_layouts=stereo"
PCM16_CEILING = 32767 / 32768
AUDIO_VARIANT_SPECS = {
    "no_vocals": ("without", "vocals"), "vocals": ("stem", "vocals"),
    "no_guitar": ("without", "guitar"), "guitar": ("stem", "guitar"),
    "no_drums": ("without", "drums"), "drums": ("stem", "drums"),
    "no_bass": ("without", "bass"), "bass": ("stem", "bass"),
    "no_piano": ("without", "piano"), "piano": ("stem", "piano"),
    "no_keys": ("without", "keys"), "keys": ("stem", "keys"),
    "no_synth": ("without", "synth"), "synth": ("stem", "synth"),
    "original": ("original", None),
}


def json_bytes(data):
    return json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()


def variant_identity(item):
    """The fixed option IDs are also the only permitted audio path components."""
    if not isinstance(item, dict):
        raise ValueError("저장할 음원 선택 정보가 올바르지 않습니다.")
    identifier, label = item.get("id"), item.get("label")
    if (
        any(key not in item for key in ("id", "label", "mode", "target"))
        or not isinstance(identifier, str) or identifier not in AUDIO_VARIANT_SPECS
        or not isinstance(label, str) or not label.strip() or len(label) > 100
        or (item.get("mode"), item.get("target")) != AUDIO_VARIANT_SPECS[identifier]
    ):
        raise ValueError("저장할 음원 이름·악기·분리 방식이 올바르지 않습니다.")
    return {key: item[key] for key in ("id", "label", "mode", "target")}


def _requested_variants(request):
    if "audio_variants" not in request:
        return None
    supplied = request["audio_variants"]
    if not isinstance(supplied, list) or not 1 <= len(supplied) <= len(AUDIO_VARIANT_SPECS):
        raise ValueError(
            f"저장할 음원을 1개 이상 선택하세요. 최대 {len(AUDIO_VARIANT_SPECS)}개까지 지원합니다."
        )
    variants, identifiers = [], set()
    for item in supplied:
        identity = variant_identity(item)
        if identity["id"] in identifiers:
            raise ValueError("저장할 음원 선택이 중복되었습니다.")
        identifiers.add(identity["id"])
        path = item.get("path")
        if not isinstance(path, str) or not path:
            raise ValueError("저장할 음원 파일이 없습니다. 먼저 분리를 완료하세요.")
        verification = {
            key: item[key] for key in (
                "expected_sha256", "expected_source_sha256", "expected_frames",
            ) if key in item
        }
        if verification and (
            len(verification) != 3
            or any(not isinstance(verification.get(key), str)
                   or not re.fullmatch(r"[a-f0-9]{64}", verification[key])
                   for key in ("expected_sha256", "expected_source_sha256"))
            or type(verification.get("expected_frames")) is not int
            or not 1 <= verification["expected_frames"] <= MAX_AUDIO_BYTES // 4
        ):
            raise ValueError("준비한 음원의 검증 정보가 올바르지 않습니다. 다시 준비하세요.")
        variants.append({**identity, "path": Path(path).resolve(strict=True), **verification})
    if request.get("default_audio_variant") not in identifiers:
        raise ValueError("처음 재생할 음원을 저장 목록에서 선택하세요.")
    return variants


def _measure_peak(source, scratch):
    """Scan one canonical float file at a time; never clip a stem before measuring it."""
    wav = scratch / "peak-float.wav"
    try:
        run_ffmpeg([
            "-i", str(source), "-map", "0:a:0", "-vn", "-af", FLOAT_FORMAT,
            "-map_metadata", "-1", "-c:a", "pcm_f32le", "-rf64", "never", str(wav),
        ])
        info = inspect_audio(wav)
        if info["frames"] * 4 > MAX_AUDIO_BYTES:
            raise ValueError("각 재생 음원은 최대 512 MB입니다. 곡을 짧게 나누어 준비하세요.")
        peak = 0.0
        with sf.SoundFile(wav) as stream:
            for block in stream.blocks(blocksize=262144, dtype="float32", always_2d=True):
                if not np.isfinite(block).all():
                    raise ValueError(
                        "음원에 유효하지 않은 샘플이 있습니다. 분리를 다시 실행하세요."
                    )
                peak = max(peak, float(np.abs(block).max()))
        return info, peak
    finally:
        wav.unlink(missing_ok=True)


def _encode_audio(source, scratch, name, audio_format, bitrate, report, gain=None):
    wav = scratch / f"{name}.wav"
    effect = [] if gain is None else [
        "-af", f"{FLOAT_FORMAT},volume={gain:.17g}:precision=double",
    ]
    run_ffmpeg([
        "-i", str(source), "-map", "0:a:0", "-vn", "-ac", "2", "-ar", "44100",
        *effect, "-map_metadata", "-1", "-c:a", "pcm_s16le", "-rf64", "never", str(wav),
    ])
    audio_info = inspect_audio(wav, scan=True)
    if wav.stat().st_size > MAX_AUDIO_BYTES or audio_info["frames"] * 4 > MAX_AUDIO_BYTES:
        raise ValueError("각 재생 음원은 최대 512 MB입니다. 곡을 짧게 나누어 준비하세요.")
    encoded = wav
    if audio_format == "mp3":
        report(f"음원을 {bitrate} kbps MP3로 압축합니다…", None)
        encoded = scratch / f"{name}.mp3"
        run_ffmpeg([
            "-i", str(wav), "-map", "0:a:0", "-vn", "-ac", "2", "-ar", "44100",
            "-map_metadata", "-1", "-c:a", "libmp3lame", "-b:a", f"{bitrate}k",
            # Keep encoder delay/padding so decoding restores the original clock.
            "-write_xing", "1", "-id3v2_version", "0", "-write_id3v1", "0", str(encoded),
        ])
        if inspect_audio(encoded) != audio_info:
            raise ValueError("MP3 변환 후 음원 길이가 달라졌습니다. WAV로 저장해 주세요.")
    if encoded.stat().st_size > MAX_AUDIO_BYTES:
        raise ValueError("각 재생 음원은 최대 512 MB입니다. 곡을 짧게 나누어 준비하세요.")
    return encoded, audio_info


def export_magic(request, report=lambda *_: None):
    audio_format = request.get("audio_format", "mp3")
    bitrate = request.get("mp3_bitrate_kbps", 256)
    if audio_format not in ("mp3", "wav"):
        raise ValueError("연습 파일 반주는 MP3 또는 WAV로 저장하세요.")
    if type(bitrate) is not int or bitrate not in (128, 192, 256, 320):
        raise ValueError("MP3 음질은 128, 192, 256, 320 kbps 중에서 선택하세요.")
    variants = _requested_variants(request)

    import pypdfium2 as pdfium

    audio, pdf, timeline_path = (
        Path(request[k]).resolve(strict=True) for k in ("audio", "pdf", "timeline")
    )
    instrumental = Path(request["instrumental"]).resolve(strict=True) if variants is None else None
    output = Path(request["output"]).resolve()
    if output.suffix.lower() != ".magic":
        raise ValueError("저장할 파일 이름은 .magic으로 끝나야 합니다.")
    if output.exists() and not request.get("overwrite"):
        raise FileExistsError("같은 이름의 파일이 있습니다. 다른 이름으로 저장하세요.")
    plan = ScoreTimeline.load(timeline_path)
    if plan.data["audio_sha256"] != hash_file(audio) or plan.data["pdf_sha256"] != hash_file(pdf):
        raise ValueError("현재 음원·악보와 분석 결과가 다릅니다. 다시 분석하세요.")
    gp = plan.data.get("gp_display")
    pdf_only = (
        plan.data.get("notation_source", {}).get("method") == "pdf-geometry"
        or plan.data.get("pdf_layout", {}).get("method") == "pdf-only-staves-barlines"
    )
    source = Path(gp["source"]).resolve(strict=True) if gp else pdf
    if gp and hash_file(source) != gp["signature"]["source"]:
        raise ValueError("GP 원본이 분석 이후 변경되었습니다. 다시 분석하세요.")
    audio_inputs = {item["path"] for item in variants} if variants is not None else {instrumental}
    inputs = {p: hash_file(p) for p in {audio, pdf, timeline_path, source, *audio_inputs}}
    for item in variants or []:
        if "expected_sha256" in item and (
            inputs[item["path"]] != item["expected_sha256"]
            or inputs[audio] != item["expected_source_sha256"]
        ):
            raise ValueError("준비한 음원 또는 원곡이 변경되었습니다. 곡을 다시 준비하세요.")
    if output in inputs:
        raise ValueError("원본 파일 위에 저장할 수 없습니다.")
    pitch = request.get("pitch_semitones", 0)
    if type(pitch) is not int or not -12 <= pitch <= 12:
        raise ValueError("반주 키 설정이 올바르지 않습니다.")
    practice = request.get("practice", {})
    defaults = {"bpm": 120, "beats": 4, "unit": 4, "origin": 0}
    practice = {k: practice.get(k, v) for k, v in defaults.items()}
    if (
        not all(type(v) in (int, float) and math.isfinite(v) for v in practice.values())
        or not 20 <= practice["bpm"] <= 400
        or not 1 <= practice["beats"] <= 16
        or practice["unit"] not in (2, 4, 8, 16)
        or not -30 <= practice["origin"] <= 1800
    ):
        raise ValueError("메트로놈 설정이 올바르지 않습니다.")
    if gp:
        from .gp_render import native_layout

        sizes, bars, _ = native_layout(gp)
        collection = prepare_collection(
            timeline_path, Paths(Path(request.get("root", ROOT))), report
        )
        for part in collection:
            for path in (Path(part["prepared"]["pdf"]), part["timing"]):
                inputs[path] = hash_file(path)
    elif pdf_only:
        from .score_geometry import detect_pdf_layout

        sizes, bars, _ = detect_pdf_layout(pdf, report=report)
        if len(plan.data.get("originals", [])) != len(bars) or any(
            cue["source_index"] not in bars for cue in plan.cues
        ):
            raise ValueError("PDF 마디 수가 시간표와 다릅니다. PDF만으로 분석을 다시 실행하세요.")
        collection = []
    else:
        from .score_layout import analyze_layout

        xml = Path(plan.data.get("musicxml_path", ""))
        if not xml.is_file() or hash_file(xml) != plan.data.get("xml_sha256"):
            raise ValueError("악보 분석 파일이 없습니다. 사전 분석을 다시 실행하세요.")
        sizes, bars, _ = analyze_layout(pdf, xml, report)
        collection = []
    portable = portable_timing(plan, bars)
    if pdf_only and not gp:
        portable["camera_mode"] = "system"
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".magic-", dir=output.parent) as scratch:
        scratch = Path(scratch)
        audio_files, portable_variants, common_audio = [], [], None
        selected = (
            variants if variants is not None else [{"id": "instrumental", "path": instrumental}]
        )
        prepared_audio, peak, gain = [], 0.0, None
        if variants is not None:
            from .transpose import prepare_pitch

            for item in selected:
                report(f"음원 음량 확인 · {item['label']}", None)
                prepared = prepare_pitch(
                    item["path"], Paths(Path(request.get("root", ROOT))), pitch, report
                )
                if prepared not in inputs:
                    inputs[prepared] = hash_file(prepared)
                info, maximum = _measure_peak(prepared, scratch)
                if "expected_frames" in item and info["frames"] != item["expected_frames"]:
                    raise ValueError("준비한 음원의 길이가 달라졌습니다. 곡을 다시 준비하세요.")
                if common_audio is not None and info != common_audio:
                    raise ValueError(
                        "선택한 음원들의 길이·샘플 수가 다릅니다. 같은 원곡에서 다시 준비하세요."
                    )
                common_audio, peak = info, max(peak, maximum)
                prepared_audio.append((item, prepared))
            # One attenuation preserves the relative level of every listening version.
            gain = PCM16_CEILING / peak if peak > PCM16_CEILING else 1.0
            if gain < 1:
                report(
                    f"음원 왜곡을 막기 위해 전체 음량을 {20 * math.log10(gain):.2f} dB 조절합니다.",
                    None,
                )
        else:
            prepared_audio = [(item, item["path"]) for item in selected]
        for item, prepared in prepared_audio:
            report(f"재생 음원 준비 · {item.get('label', '반주')}", None)
            encoded, audio_info = _encode_audio(
                prepared, scratch, item["id"], audio_format, bitrate, report, gain
            )
            if common_audio is not None and audio_info != common_audio:
                raise ValueError(
                    "선택한 음원들의 길이·샘플 수가 다릅니다. 같은 원곡에서 다시 준비하세요."
                )
            common_audio = audio_info
            prefix = "audio/variants" if variants is not None else "audio"
            audio_name = f"{prefix}/{item['id']}.{audio_format}"
            descriptor = {
                "path": audio_name, **audio_info, "pitch_semitones": pitch,
                "source_sha256": plan.data["audio_sha256"],
            }
            if audio_format == "mp3":
                descriptor.update(codec="mp3", bitrate_kbps=bitrate)
            elif variants is not None:
                descriptor["codec"] = "wav"
            audio_files.append((audio_name, encoded))
            if sum(path.stat().st_size for _, path in audio_files) >= MAX_BUNDLE_BYTES:
                raise ValueError(
                    "연습 파일은 최대 1 GB입니다. 저장할 음원 수나 음질을 줄여 주세요."
                )
            if variants is not None:
                portable_variants.append({**variant_identity(item), "audio": descriptor})
            else:
                default_audio = descriptor
        if variants is not None:
            default_audio = next(
                item["audio"] for item in portable_variants
                if item["id"] == request["default_audio_variant"]
            )
        entries, parts = {}, []
        bundle = scratch / "export.magic"
        with zipfile.ZipFile(
            bundle, "w", compression=zipfile.ZIP_STORED, allowZip64=False
        ) as archive:

            def put(name, data):
                from hashlib import sha256

                if isinstance(data, Path):
                    entries[name] = {"bytes": data.stat().st_size, "sha256": hash_file(data)}
                    archive.write(data, name)
                else:
                    entries[name] = {"bytes": len(data), "sha256": sha256(data).hexdigest()}
                    archive.writestr(name, data)
                if archive.fp.tell() > MAX_BUNDLE_BYTES:
                    raise ValueError("연습 파일은 최대 1 GB입니다. 곡이나 GP 파일을 나누어 주세요.")

            def put_pages(document_path, prefix, part_name):
                pages = []
                document = pdfium.PdfDocument(document_path)
                try:
                    if not 1 <= len(document) <= 200:
                        raise ValueError("각 파트의 악보는 최대 200페이지까지 지원합니다.")
                    for i in range(len(document)):
                        report(f"{part_name} · {i + 1}/{len(document)}쪽 저장", None)
                        page = document[i]
                        try:
                            width, height = page.get_size()
                            bitmap = page.render(scale=2200 / max(width, height))
                            try:
                                picture = bitmap.to_pil()
                                buffer = io.BytesIO()
                                picture.save(buffer, format="PNG")
                                name = f"{prefix}/{i + 1:04d}.png"
                                put(name, buffer.getvalue())
                                pages.append(
                                    {"path": name, "width": picture.width, "height": picture.height}
                                )
                            finally:
                                bitmap.close()
                        finally:
                            page.close()
                finally:
                    document.close()
                return pages

            for audio_name, encoded in audio_files:
                put(audio_name, encoded)
            source_name = "score/source" + source.suffix.lower()
            put(source_name, source)
            put("timeline.json", json_bytes(portable))
            pages = put_pages(pdf, "pages", "선택한 악보")
            for part in collection:
                prepared = part["prepared"]
                track = prepared["signature"]["track"]
                name = prepared["metadata"]["track_name"]
                if track == gp["signature"]["track"]:
                    part_pages, timeline_name = pages, "timeline.json"
                else:
                    prefix = f"parts/{track:02d}"
                    timeline_name = f"{prefix}/timeline.json"
                    put(timeline_name, part["timing"])
                    part_pages = put_pages(prepared["pdf"], f"{prefix}/pages", name)
                parts.append({
                    "track": track, "name": name,
                    "percussion": bool(prepared["metadata"]["percussion"]),
                    "pages": part_pages, "timeline": timeline_name,
                })
            manifest = {
                "format": "Magic-Instrumental",
                "version": MAGIC_VERSION if variants is not None else (
                    3 if audio_format == "mp3" else (2 if gp else 1)
                ),
                "title": str(request.get("title") or audio.stem)[:200],
                "audio": default_audio,
                "score": {
                    "kind": "gp" if gp else "pdf",
                    "path": source_name,
                    "part": plan.data.get("notation_source", {}).get("track_name", "")
                    if gp
                    else "",
                    "track": gp["signature"]["track"] if gp else None,
                },
                "timeline": "timeline.json",
                "pages": pages,
                "practice": practice,
                "entries": entries,
            }
            if variants is not None:
                manifest["audio_gain"] = gain
                manifest["audio_variants"] = portable_variants
                manifest["default_audio_variant"] = request["default_audio_variant"]
            if gp:
                manifest["parts"] = parts
                manifest["timing_basis"] = {"track": gp["signature"]["track"], "shared": True}
            archive.writestr("manifest.json", json_bytes(manifest))
        if bundle.stat().st_size > MAX_BUNDLE_BYTES:
            raise ValueError("연습 파일은 최대 1 GB입니다. 악보·곡을 나누어 준비하세요.")
        if any(hash_file(p) != digest for p, digest in inputs.items()):
            raise ValueError("저장 중 원본이 변경되었습니다. 다시 시도해 주세요.")
        publish_export(bundle, output, overwrite=bool(request.get("overwrite")))
        report(".magic 저장 완료 · PC 또는 웹 재생기에서 열어 주세요.", 1)
    return output
