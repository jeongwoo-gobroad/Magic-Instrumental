# SPDX-License-Identifier: GPL-3.0-only
"""Worker-only GP part preparation with one shared, reviewed media timeline."""

import json
from hashlib import sha256
from pathlib import Path

from filelock import FileLock

from .cache import hash_file
from .score import ScoreTimeline, atomic_json

COLLECTION_VERSION = 1
MAX_COLLECTION_PAGES = 1000


def portable_timing(plan, bars, *, preserve_pages=True):
    cues = []
    for cue in plan.cues:
        box = bars.get(cue["source_index"])
        if not preserve_pages and not box:
            raise ValueError("파트의 마디 위치가 없습니다. 다시 분석하세요.")
        page = cue["page"] if preserve_pages else box["page"]
        if not box or box["page"] != page:
            box = {"left": 0, "right": 1, "top": 0, "bottom": 1, "next_top": 0}
        cues.append(
            {k: cue[k] for k in ("start", "measure", "visit", "source_index")}
            | {"page": page,
               "box": {k: float(box[k]) for k in ("left", "right", "top", "bottom", "next_top")}}
        )
    return {
        "cues": cues, "end": plan.end, "offset": plan.data.get("offset", 0),
        "reviewed": bool(plan.data.get("reviewed")), "warnings": plan.data.get("warnings", []),
    }


def prepare_all_gp(source, paths, selected=1, report=lambda *_: None):
    from .gp_render import prepare_gp
    from .guitar_pro import list_tracks

    tracks = list_tracks(source)
    if selected not in {t["number"] for t in tracks}:
        raise ValueError("선택한 GP 파트가 없습니다.")
    prepared = {}
    for track in sorted(tracks, key=lambda t: t["number"] != selected):
        number = track["number"]
        report(f"GP 전체 파트 준비 · {number}/{len(tracks)} · {track['name']}", None)
        try:
            prepared[number] = prepare_gp(source, paths, number, report)
        except ValueError as error:
            raise ValueError(f"GP {number}번 파트({track['name']}) 준비 실패: {error}") from error
    if sum(len(p["page_sizes"]) for p in prepared.values()) > MAX_COLLECTION_PAGES:
        raise ValueError("GP 전체 파트는 합계 1,000페이지까지 지원합니다.")
    return [prepared[t["number"]] for t in tracks]


def _route(cues):
    return [(c["source_index"], c["visit"], c["beat"], c["beats"]) for c in cues]


def prepare_collection(timeline_path, paths, report=lambda *_: None):
    """Analyze every part's notation/route, reusing the selected part's media times.

    Independent DTW on silent/drum parts would produce conflicting clocks. Require
    identical performed musical structure before sharing the reference timings.
    """
    from .gp_render import GP_VERSION, RENDER_VERSION, native_layout
    from .score_analysis import parse_notation

    timeline_path = Path(timeline_path)
    reference_hash = hash_file(timeline_path)
    plan = ScoreTimeline.load(timeline_path)
    gp = plan.data.get("gp_display")
    if not gp:
        raise ValueError("GP 원본에서 만든 악보 시간표가 필요합니다.")
    source = Path(gp["source"])
    if hash_file(source) != gp["signature"]["source"]:
        raise ValueError("GP 원본이 분석 이후 변경되었습니다. 다시 분석하세요.")
    selected = gp["signature"]["track"]
    prepared = prepare_all_gp(source, paths, selected, report)
    signature = {
        "version": COLLECTION_VERSION, "reference": reference_hash,
        "source": gp["signature"]["source"], "gp": GP_VERSION, "render": RENDER_VERSION,
        "parts": [p["signature"] for p in prepared],
    }
    key = sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
    folder = paths.processed / "gp-collections" / signature["source"] / key
    folder.mkdir(parents=True, exist_ok=True)
    with FileLock(str(folder / ".collection.lock"), timeout=3600):
        manifest = folder / "collection.json"
        if manifest.is_file():
            try:
                saved = json.loads(manifest.read_text())
                expected = {f"track-{p['signature']['track']}.json" for p in prepared}
                if (saved["signature"] == signature and set(saved["files"]) == expected and all(
                    hash_file(folder / name) == digest for name, digest in saved["files"].items()
                ) and hash_file(timeline_path) == reference_hash
                        and hash_file(source) == signature["source"]):
                    return [
                        {"prepared": p, "timing": folder / f"track-{p['signature']['track']}.json"}
                        for p in prepared
                    ]
            except (OSError, ValueError, KeyError, TypeError):
                pass
        files, result = {}, []
        for part in prepared:
            number = part["signature"]["track"]
            report(f"전체 파트의 마디·반복 대조 · {part['metadata']['track_name']}", None)
            sizes, bars, _ = native_layout(part)
            xml = Path(part["pdf"]).with_name("score.musicxml")
            if hash_file(xml) != part["files"]["score.musicxml"]:
                raise ValueError("GP 분석 파일이 변경되었습니다. 다시 준비하세요.")
            notation = parse_notation(
                xml, len(sizes), plan.data.get("signature", {}).get("route", ""),
                measure_pages={i: b["page"] for i, b in bars.items()},
            )
            if _route(notation["cues"]) != _route(plan.cues):
                raise ValueError(
                    f"{part['metadata']['track_name']}의 마디·박자·반복 순서가 "
                    "기준 파트와 다릅니다. "
                    "같은 연주 순서로 보정한 뒤 다시 분석하세요."
                )
            if number == selected and part["signature"] != gp["signature"]:
                raise ValueError("GP 분석 버전이 바뀌었습니다. 기준 파트를 다시 분석하세요.")
            timing = portable_timing(plan, bars, preserve_pages=number == selected)
            timing["warnings"] = sorted(set(timing["warnings"] + notation["warnings"]))
            name = f"track-{number}.json"
            atomic_json(folder / name, timing)
            files[name] = hash_file(folder / name)
            result.append({"prepared": part, "timing": folder / name})
        if (hash_file(source) != signature["source"]
                or hash_file(timeline_path) != reference_hash):
            raise ValueError("전체 파트 준비 중 원본 또는 시간표가 변경되었습니다.")
        atomic_json(manifest, {"signature": signature, "files": files})
        return result
