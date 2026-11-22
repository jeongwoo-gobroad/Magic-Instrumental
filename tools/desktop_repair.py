# SPDX-License-Identifier: GPL-3.0-only
"""Apply the verified 2.4.5 follow-resume edition from its separate Repair ZIP."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path, PurePosixPath

import update

VERSION = "2.4.5"
REVISION = "2.4.5-follow-resume-1"
PREFIX = f"Magic-Instrumental-{REVISION}"
SOURCE_NAME = f"{PREFIX}.zip"
BASELINE_COMMIT = "099737bc850ce55d98175095d4f956e5d437bd30"
BASELINE_MANIFEST_SHA256 = "c0983e8d1e9e0c8173d36ccb920d87359fa20b88c7d401707a105e5824c8c3db"
SOURCE_URL = update.RELEASES + f"v{VERSION}/{SOURCE_NAME}"
PROFILE_LIMIT = 2 * 1024 * 1024


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def inventory(manifest: dict) -> dict[str, str]:
    if manifest.get("version") != VERSION or not isinstance(manifest.get("files"), list):
        raise ValueError("수정판의 프로그램 파일 목록이 올바르지 않습니다.")
    files, seen = {}, set()
    for record in manifest["files"]:
        name, sha = record.get("path"), record.get("sha256")
        if (
            not isinstance(name, str) or not update.public_path(name)
            or name.casefold() in seen or not isinstance(sha, str)
            or not re.fullmatch("[a-f0-9]{64}", sha)
        ):
            raise ValueError("수정판에 허용되지 않은 프로그램 파일이 있습니다.")
        seen.add(name.casefold())
        if PurePosixPath(name).parts[0] not in update.DATA_DIRS:
            files[name] = sha
    if not {"main.py", "bootstrap.py", "practice_player/__init__.py"} <= files.keys():
        raise ValueError("수정판의 필수 프로그램 파일이 빠졌습니다.")
    return files


def validate_profile(profile: dict) -> tuple[dict, dict]:
    if (
        not isinstance(profile, dict)
        or profile.get("format") != "Magic-Instrumental-Desktop-Repair"
        or profile.get("revision") != REVISION or profile.get("version") != VERSION
        or profile.get("baseline_commit") != BASELINE_COMMIT
        or not isinstance(profile.get("source_commit"), str)
        or not re.fullmatch("[a-f0-9]{40}", profile["source_commit"])
        or profile.get("source_url") != (
            f"https://github.com/{update.REPO}/commit/{profile['source_commit']}"
        )
        or profile.get("archive_name") != SOURCE_NAME
        or type(profile.get("archive_bytes")) is not int
        or not 0 < profile["archive_bytes"] <= update.MAX_ARCHIVE
        or not isinstance(profile.get("archive_sha256"), str)
        or not re.fullmatch("[a-f0-9]{64}", profile["archive_sha256"])
    ):
        raise ValueError("공식 v2.4.5 수정판 정보가 아닙니다.")
    baseline_text, target_text = profile.get("baseline_manifest"), profile.get("manifest")
    if not isinstance(baseline_text, str) or not isinstance(target_text, str):
        raise ValueError("수정판 파일 목록을 확인할 수 없습니다.")
    if digest(baseline_text.encode()) != BASELINE_MANIFEST_SHA256:
        raise ValueError("원본 v2.4.5 파일 목록의 검증에 실패했습니다.")
    if digest(target_text.encode()) != profile.get("manifest_sha256"):
        raise ValueError("수정판 파일 목록의 검증에 실패했습니다.")
    baseline, target = json.loads(baseline_text), json.loads(target_text)
    inventory(baseline)
    inventory(target)
    return baseline, target


def load_profile(path: Path) -> dict:
    if path.stat().st_size > PROFILE_LIMIT:
        raise ValueError("수정판 정보의 크기가 너무 큽니다.")
    profile = json.loads(path.read_bytes())
    validate_profile(profile)
    return profile


def already_applied(root: Path, baseline: dict, target: dict) -> bool:
    old, new = inventory(baseline), inventory(target)
    complete = True
    for name in sorted(old.keys() | new.keys()):
        path = update.safe_target(root, name)
        if not path.exists():
            if name in old:
                raise ValueError(f"원본 프로그램 파일이 없습니다: {name}")
            complete = False
            continue
        current = digest(path.read_bytes()) if path.is_file() else None
        if current is None or current not in {old.get(name), new.get(name)}:
            raise ValueError(
                f"원본 v2.4.5 또는 이 수정판과 다른 프로그램 파일입니다: {name}. "
                "직접 수정하거나 이후에 받은 코드는 자동 교체하지 않습니다."
            )
        if name in new and current != new[name]:
            complete = False
    return complete


def repair(root: Path, profile: dict, *, wait_seconds=0, installer=None):
    baseline, target = validate_profile(profile)
    root = root.resolve()
    if (root / ".git").exists():
        raise ValueError("개발용 Git 폴더입니다. 일반 ZIP 설치 폴더를 선택하세요.")
    for name in ("main.py", "bootstrap.py", "practice_player/__init__.py"):
        if not update.safe_target(root, name).is_file():
            raise ValueError("기존 Magic-Instrumental 폴더를 선택하세요.")
    if (
        sys.prefix == sys.base_prefix or root == Path(sys.prefix).resolve()
        or root in Path(sys.prefix).resolve().parents
    ):
        raise ValueError("기존 프로그램의 외부 가상환경으로 실행해야 합니다.")
    installer = installer or update.setup
    with update.update_lock(root, wait_seconds):
        if update.version(update.installed_version(root)) > update.version(VERSION):
            raise ValueError("더 새로운 버전에는 이 v2.4.5 수정판을 적용하지 않습니다.")
        work_root = update.safe_target(root, ".magic-updates")
        work_root.mkdir(exist_ok=True)
        update.restore_pending(root, installer)
        current = update.installed_version(root)
        if update.version(current) > update.version(VERSION):
            raise ValueError("더 새로운 버전에는 이 v2.4.5 수정판을 적용하지 않습니다.")
        same_version = update.version(current) == update.version(VERSION)
        if same_version and already_applied(root, baseline, target):
            print("v2.4.5 따라가기 수정판이 이미 적용되어 있습니다. 변경하지 않았습니다.",
                  flush=True)
            return False
        if shutil.disk_usage(root).free < update.MAX_EXPANDED * 3:
            raise ValueError("수정판 적용에 약 500 MB의 여유 공간이 필요합니다.")
        with tempfile.TemporaryDirectory(prefix="repair-", dir=work_root) as temporary:
            work = Path(temporary)
            archive = work / SOURCE_NAME
            data = update.download(SOURCE_URL, archive, profile["archive_bytes"])
            if len(data) != profile["archive_bytes"] or digest(data) != profile["archive_sha256"]:
                raise ValueError("수정판 ZIP 검증에 실패했습니다. 기존 파일은 유지됩니다.")
            stage = work / "stage"
            names = update.stage_release(archive, target, stage)
            update.check_legacy_processes(root)
            # Recheck after downloading so a concurrent manual edit is preserved as well.
            if update.installed_version(root) != current:
                raise ValueError("확인 중 프로그램 버전이 바뀌었습니다. 변경하지 않았습니다.")
            if same_version:
                already_applied(root, baseline, target)
            print(f"v{current} → v{VERSION} 따라가기 수정판 적용 중입니다.", flush=True)
            update.apply_release(root, stage, names, installer)
        print("수정판 적용 완료! 기존 폴더의 start 파일로 실행하세요.", flush=True)
        return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--pause", action="store_true")
    parser.add_argument("--wait-for-app", action="store_true")
    args = parser.parse_args()
    result = 0
    try:
        profile = load_profile(Path(__file__).with_name("repair-profile.json"))
        repair(args.project, profile, wait_seconds=60 if args.wait_for_app else 0)
    except (Exception, KeyboardInterrupt) as error:
        print(f"수정판을 적용하지 못했습니다: {error}", file=sys.stderr, flush=True)
        print("개인 자료는 삭제하지 않았습니다. 프로그램을 닫고 수정판 도구를 다시 실행하세요.",
              file=sys.stderr, flush=True)
        result = 1
    if args.pause and os.environ.get("MAGIC_NO_PAUSE") != "1" and sys.stdin.isatty():
        try:
            input("Enter 키를 누르면 창이 닫힙니다. ")
        except (EOFError, KeyboardInterrupt):
            pass
    return result


if __name__ == "__main__":
    raise SystemExit(main())
