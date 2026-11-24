# SPDX-License-Identifier: GPL-3.0-only
import argparse
import json
import os
import signal
import sys
import traceback
from pathlib import Path

from . import __version__
from .config import MODELS, ROOT, Paths, Settings
from .variants import SUPPORTED_VARIANTS

EVENT_PREFIX = "@@PRACTICE@@"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Magic-Instrumental · 인스트루먼탈 연습실")
    result.add_argument("--version", action="version", version=f"Magic-Instrumental {__version__}")
    sub = result.add_subparsers(dest="command")
    sub.add_parser("gui", help="GUI 실행 (기본)")
    sub.add_parser("doctor", help="가상환경·GPU·FFmpeg 확인")
    split = sub.add_parser("separate", help="분리 또는 캐시 불러오기")
    group = split.add_mutually_exclusive_group(required=True)
    group.add_argument("--input", type=Path)
    group.add_argument("--all", action="store_true")
    split.add_argument("--model", choices=["auto", *MODELS], default="auto")
    split.add_argument(
        "--variant", default="no_vocals",
        choices=SUPPORTED_VARIANTS,
        help="without/no_ 옵션은 해당 악기를 제외하고, 악기 이름은 해당 악기만 준비",
    )
    split.add_argument(
        "--all-variants", action="store_true", help="지원하는 음원 선택지를 함께 준비"
    )
    split.add_argument("--device", choices=["auto", "cuda", "mps", "cpu"], default="auto")
    split.add_argument("--low-memory", action="store_true")
    split.add_argument("--offline", action="store_true")
    split.add_argument("--root", type=Path, default=ROOT)
    split.add_argument("--events", action="store_true", help=argparse.SUPPRESS)
    export = sub.add_parser("export", help="음정을 유지한 속도 변경 파일 저장")
    export.add_argument("--input", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--speed", type=float, required=True)
    export.add_argument("--overwrite", action="store_true", help="기존 내보내기 파일 교체 허용")
    export.add_argument("--events", action="store_true", help=argparse.SUPPRESS)
    score = sub.add_parser("score-analyze", help="PDF 기호 인식 및 음원 동기화 사전 분석")
    score.add_argument("--audio", type=Path, required=True)
    score.add_argument("--pdf", type=Path)
    score.add_argument("--gp", type=Path, help="PDF 없이 GP 파트로 표시·분석")
    score.add_argument("--musicxml", "--notation", type=Path, help="MusicXML 또는 GP3/4/5")
    score.add_argument("--gp-track", type=int, default=1, help="Guitar Pro 트랙 번호 (1부터)")
    score.add_argument("--mode", choices=["auto", "audio", "tempo", "pdf"], default="auto",
                       help="auto: 음표 인식 후 실패하면 마디선·BPM 사용; pdf: 마디선만 사용")
    score.add_argument("--bpm", type=float, default=120)
    score.add_argument("--meter-beats", type=int, default=4, help="PDF 모드 박자표 분자 (1~32)")
    score.add_argument("--meter-unit", type=int, default=4,
                       choices=[1, 2, 4, 8, 16, 32], help="PDF 모드 박자표 분모")
    score.add_argument("--lead-in", type=float, default=0)
    score.add_argument("--route", default="")
    score.add_argument("--force", action="store_true")
    score.add_argument("--zoom", help="page, width 또는 배율 0.5~6 (예: 2.5)")
    score.add_argument("--view-width", type=int, default=800)
    score.add_argument("--view-height", type=int, default=650)
    score.add_argument("--base-timeline", type=Path, help="기존 마디 시간표로 스크롤만 재분석")
    score.add_argument("--root", type=Path, default=ROOT)
    score.add_argument("--events", action="store_true", help=argparse.SUPPRESS)
    gp = sub.add_parser("gp-prepare", help="GP 파트 악보 준비")
    gp.add_argument("--gp", type=Path, required=True)
    gp.add_argument("--gp-track", type=int, default=1)
    pitch = sub.add_parser("pitch", help="반주 키 변경")
    pitch.add_argument("--input", type=Path, required=True)
    pitch.add_argument("--semitones", type=int, required=True)
    pitch.add_argument("--expected-sha256", help=argparse.SUPPRESS)
    pitch.add_argument("--expected-frames", type=int, help=argparse.SUPPRESS)
    for command in (gp, pitch):
        command.add_argument("--root", type=Path, default=ROOT)
        command.add_argument("--events", action="store_true", help=argparse.SUPPRESS)
    magic = sub.add_parser("magic-export", help="PC·태블릿용 .magic 저장")
    magic.add_argument("--request", type=Path, required=True)
    magic.add_argument("--events", action="store_true", help=argparse.SUPPRESS)
    magic_import = sub.add_parser("magic-import", help=".magic 검증 및 PC 재생 준비")
    magic_import.add_argument("--input", type=Path, required=True)
    magic_import.add_argument("--root", type=Path, default=ROOT)
    magic_import.add_argument("--events", action="store_true", help=argparse.SUPPRESS)
    return result


def prepare_audio_options(source, model, device, low_memory, paths, report, *,
                          variant_id="no_vocals", prepare_all=False, offline=False):
    """Choose each recommended model once, then combine only its assigned options."""
    from dataclasses import replace

    from .pipeline import process
    from .variants import recommended_model, supported_variants

    wanted = (
        tuple(SUPPORTED_VARIANTS) if model == "auto" else supported_variants(model)
    ) if prepare_all else (variant_id,)
    if variant_id not in wanted:
        raise ValueError("선택한 모델이 이 음원을 지원하지 않습니다. 자동 추천을 선택하세요.")
    assignments = {
        identifier: (
            "original" if identifier == "original" and not prepare_all
            else recommended_model(identifier) if model == "auto" else model
        )
        for identifier in wanted
    }
    variants, results = {}, []
    for key in dict.fromkeys(assignments.values()):
        assigned = [identifier for identifier, owner in assignments.items() if owner == key]
        result = process(
            source, Settings(key, device, low_memory), paths, report, offline=offline,
            variant_id=variant_id if variant_id in assigned else assigned[0],
            prepare_all=prepare_all,
        )
        if results and (
            result.source_hash != results[0].source_hash
            or result.source_frames != results[0].source_frames
            or result.sample_rate != results[0].sample_rate
        ):
            raise ValueError("준비 중 원곡 또는 음원 길이가 달라졌습니다. 다시 준비하세요.")
        results.append(result)
        for identifier in assigned:
            if identifier not in result.variants:
                raise ValueError("선택한 음원 결과가 없습니다. 분리 모델과 로그를 확인하세요.")
            variants[identifier] = result.variants[identifier]
        if not prepare_all:
            for identifier, item in result.variants.items():
                if model != "auto" or recommended_model(identifier) == key:
                    variants[identifier] = item
    variants = {identifier: variants[identifier] for identifier in SUPPORTED_VARIANTS
                if identifier in variants}
    selected = variants[variant_id]
    return replace(
        results[0], instrumental=selected.path, selected_variant=variant_id,
        variants=variants, cached=all(result.cached for result in results),
        device=next((result.device for result in results if not result.cached), results[0].device),
    )


def doctor() -> int:
    import platform

    import torch

    from .audio import ffmpeg
    from .engine import select_device
    from .score_analysis import audiveris_path

    data = {
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "virtual_environment": sys.prefix != sys.base_prefix,
        "platform": platform.platform(),
        "torch": torch.__version__,
        "cuda_available": torch.cuda.is_available(),
        "cuda_runtime": torch.version.cuda,
        "mps_available": torch.backends.mps.is_available(),
        "selected_device": select_device("auto"),
        "ffmpeg": ffmpeg(),
        "project": str(ROOT),
        "audiveris": audiveris_path(),
    }
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def main(argv=None) -> int:
    os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
    os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    args = parser().parse_args(argv)

    def emit(kind, **data):
        if getattr(args, "events", False):
            print(EVENT_PREFIX + json.dumps({"type": kind, **data}, ensure_ascii=False), flush=True)
        elif "message" in data:
            print(data["message"], flush=True)

    def progress(message, fraction):
        emit("progress", message=message, fraction=fraction)

    try:
        if args.command in (None, "gui"):
            from .gui import run

            return run()
        if args.command == "doctor":
            return doctor()
        if args.command == "export":
            from .audio import export_tempo

            path = export_tempo(args.input, args.output, args.speed, overwrite=args.overwrite)
            emit("exported", path=str(path), message=f"저장 완료: {path}")
            return 0
        if hasattr(signal, "SIGTERM"):

            def cancelled(*_):
                raise KeyboardInterrupt

            signal.signal(signal.SIGTERM, cancelled)
        if args.command == "magic-export":
            from .magic_bundle import export_magic

            path = export_magic(json.loads(args.request.read_text(encoding="utf-8")), progress)
            emit("magic_result", path=str(path))
            return 0
        if args.command == "magic-import":
            from .magic_import import import_magic

            imported = import_magic(args.input, Paths(args.root.resolve()), progress)
            emit("magic_imported", **imported)
            return 0
        if args.command == "gp-prepare":
            from .gp_collection import prepare_all_gp

            parts = prepare_all_gp(args.gp, Paths(args.root.resolve()), args.gp_track, progress)
            info = next(p for p in parts if p["signature"]["track"] == args.gp_track)
            emit("gp_prepared", **{k: v for k, v in info.items() if k not in {"bars", "files"}})
            return 0
        if args.command == "pitch":
            from .audio import inspect_audio
            from .cache import hash_file
            from .transpose import prepare_pitch

            if args.expected_sha256 is not None:
                import re

                if not re.fullmatch(r"[a-f0-9]{64}", args.expected_sha256):
                    raise ValueError("저장된 음원 검증 정보가 올바르지 않습니다.")
                if hash_file(args.input) != args.expected_sha256:
                    raise ValueError(
                        "저장된 음원이 변경되었습니다. 곡을 다시 준비하거나 .magic을 다시 여세요."
                    )
                info = inspect_audio(args.input, scan=True)
                if (info["frames"] != args.expected_frames or info["sample_rate"] != 44100):
                    raise ValueError("저장된 음원의 길이가 다릅니다. 곡을 다시 준비하세요.")
            path = prepare_pitch(args.input, Paths(args.root.resolve()), args.semitones, progress)
            if args.expected_sha256 is not None and hash_file(args.input) != args.expected_sha256:
                raise ValueError("준비 중 음원이 변경되었습니다. 다시 시도하세요.")
            if args.expected_sha256 is not None:
                prepared = inspect_audio(path)
                if (prepared["frames"] != args.expected_frames
                        or prepared["sample_rate"] != 44100 or prepared["channels"] != 2):
                    raise ValueError("준비한 음원의 재생 길이가 다릅니다. 곡을 다시 준비하세요.")
            emit("pitch_result", path=str(path), semitones=args.semitones)
            return 0
        if args.command == "score-analyze":
            from .score_analysis import analyze_score
            from .score_layout import analyze_scroll
            from .score_view import view_profile

            if args.mode == "pdf" and (args.gp or args.musicxml):
                raise ValueError(
                    "PDF 마디선 모드는 --pdf만 사용합니다. --gp/--musicxml은 빼 주세요."
                )
            paths = Paths(args.root.resolve())
            paths.create()
            gp_display = None
            if args.gp:
                from .gp_render import prepare_gp

                prepared = prepare_gp(args.gp, paths, args.gp_track, progress)
                gp_display = {k: prepared[k] for k in ("pdf", "layout", "source", "signature")}
                args.pdf, args.musicxml = Path(prepared["pdf"]), args.gp
            if args.pdf is None:
                raise ValueError("PDF 또는 GP 악보를 선택하세요.")
            profile = (
                view_profile(args.zoom, args.view_width, args.view_height) if args.zoom else None
            )
            if args.base_timeline and not profile:
                raise ValueError("기존 시간표로 스크롤을 분석하려면 --zoom을 지정하세요.")
            path = args.base_timeline or analyze_score(
                args.audio,
                args.pdf,
                paths,
                progress,
                musicxml=args.musicxml,
                gp_track=args.gp_track,
                gp_display=gp_display,
                mode=args.mode,
                bpm=args.bpm,
                meter_beats=args.meter_beats,
                meter_unit=args.meter_unit,
                lead_in=args.lead_in,
                route_override=args.route,
                force=args.force,
                publish=profile is None,
            )
            if profile:
                path = analyze_scroll(
                    args.audio, args.pdf, paths, path, profile, progress, musicxml=args.musicxml,
                )
            if args.gp:
                from .gp_collection import prepare_collection

                prepare_collection(path, paths, progress)
            emit("score_result", path=str(path), message=f"악보 분석 저장: {path}")
            return 0
        from .audio import discover

        paths = Paths(args.root.resolve())
        paths.create()
        sources = discover(paths.targets) if args.all else [args.input]
        if not sources:
            raise ValueError(f"음원이 없습니다. {paths.targets}에 음원을 넣으세요.")
        failed = 0
        for source in sources:
            try:
                result = prepare_audio_options(
                    source, args.model, args.device, args.low_memory, paths, progress,
                    variant_id=args.variant, prepare_all=args.all_variants, offline=args.offline,
                )
                emit("result", **result.to_dict(), message=f"준비 완료: {result.instrumental}")
            except Exception as error:
                failed += 1
                emit("error", message=f"{source.name}: {error}")
                traceback.print_exc(file=sys.stderr)
        return 1 if failed else 0
    except KeyboardInterrupt:
        emit("cancelled", message="작업을 취소했습니다.")
        return 130
    except Exception as error:
        emit("error", message=str(error))
        traceback.print_exc(file=sys.stderr)
        return 1
