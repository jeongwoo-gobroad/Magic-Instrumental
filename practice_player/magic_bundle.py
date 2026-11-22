# SPDX-License-Identifier: GPL-3.0-only
"""Portable, stored ZIP: original score, rendered pages, audio and reviewed timing."""

import io
import json
import math
import tempfile
import zipfile
from pathlib import Path

from .audio import inspect_audio, publish_export, run_ffmpeg
from .cache import hash_file
from .config import ROOT, Paths
from .gp_collection import portable_timing, prepare_collection
from .score import ScoreTimeline

MAGIC_VERSION = 3
MAX_AUDIO_BYTES = 512 * 1024 * 1024
MAX_BUNDLE_BYTES = 1024 * 1024 * 1024


def json_bytes(data):
    return json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()


def export_magic(request, report=lambda *_: None):
    audio_format = request.get("audio_format", "mp3")
    bitrate = request.get("mp3_bitrate_kbps", 256)
    if audio_format not in ("mp3", "wav"):
        raise ValueError("연습 파일 반주는 MP3 또는 WAV로 저장하세요.")
    if type(bitrate) is not int or bitrate not in (128, 192, 256, 320):
        raise ValueError("MP3 음질은 128, 192, 256, 320 kbps 중에서 선택하세요.")

    import pypdfium2 as pdfium

    audio, instrumental, pdf, timeline_path = (
        Path(request[k]).resolve(strict=True) for k in ("audio", "instrumental", "pdf", "timeline")
    )
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
    inputs = {p: hash_file(p) for p in {audio, instrumental, pdf, timeline_path, source}}
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
        report("태블릿에서 재생할 반주를 준비합니다…", None)
        wav = scratch / "instrumental.wav"
        run_ffmpeg(
            [
                "-i",
                str(instrumental),
                "-map",
                "0:a:0",
                "-vn",
                "-ac",
                "2",
                "-ar",
                "44100",
                "-map_metadata",
                "-1",
                "-c:a",
                "pcm_s16le",
                "-rf64",
                "never",
                str(wav),
            ]
        )
        audio_info = inspect_audio(wav, scan=True)
        if wav.stat().st_size > MAX_AUDIO_BYTES:
            raise ValueError("태블릿용 반주는 최대 512 MB입니다. 곡을 짧게 나누어 준비하세요.")
        encoded = wav
        if audio_format == "mp3":
            report(f"반주를 {bitrate} kbps MP3로 압축합니다…", None)
            encoded = scratch / "instrumental.mp3"
            run_ffmpeg(
                [
                    "-i", str(wav), "-map", "0:a:0", "-vn", "-ac", "2", "-ar", "44100",
                    "-map_metadata", "-1", "-c:a", "libmp3lame", "-b:a", f"{bitrate}k",
                    # Keep encoder delay/padding so decoding restores the original clock.
                    "-write_xing", "1", "-id3v2_version", "0", "-write_id3v1", "0",
                    str(encoded),
                ]
            )
            if inspect_audio(encoded) != audio_info:
                raise ValueError("MP3 변환 후 반주 길이가 달라졌습니다. WAV로 저장해 주세요.")
        audio_name = f"audio/instrumental.{audio_format}"
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
                "version": MAGIC_VERSION if audio_format == "mp3" else (2 if gp else 1),
                "title": str(request.get("title") or audio.stem)[:200],
                "audio": {
                    "path": audio_name, **audio_info, "pitch_semitones": pitch,
                    "source_sha256": plan.data["audio_sha256"],
                },
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
            if audio_format == "mp3":
                manifest["audio"].update(codec="mp3", bitrate_kbps=bitrate)
            if gp:
                manifest["parts"] = parts
                manifest["timing_basis"] = {"track": gp["signature"]["track"], "shared": True}
            archive.writestr("manifest.json", json_bytes(manifest))
        if bundle.stat().st_size > MAX_BUNDLE_BYTES:
            raise ValueError("연습 파일은 최대 1 GB입니다. 악보·곡을 나누어 준비하세요.")
        if any(hash_file(p) != digest for p, digest in inputs.items()):
            raise ValueError("저장 중 원본이 변경되었습니다. 다시 시도해 주세요.")
        publish_export(bundle, output, overwrite=bool(request.get("overwrite")))
        report(".magic 저장 완료 · 태블릿 재생기에서 열어 주세요.", 1)
    return output
