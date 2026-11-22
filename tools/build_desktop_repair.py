# SPDX-License-Identifier: GPL-3.0-only
"""Build the additive 2.4.5 repair; run with python -m tools.build_desktop_repair."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile
from pathlib import Path

from tools import build_release
from tools import desktop_repair as repair

ORIGINAL_ASSETS = {
    "Magic-Instrumental-2.4.5.zip": (
        778913348, 2562749,
        "f6d7ca1315ae56395ae0b219fc441714dfe2bf2705d704feb6ceba0904305450",
    ),
    "Magic-Instrumental-Updater.zip": (
        778913337, 24717,
        "7df31d57a958b765742045aa1abf5223fe030495c702675e47cd0735d83751e8",
    ),
    "PUBLIC_FILES.json": (778913343, 22074, repair.BASELINE_MANIFEST_SHA256),
    "SHA256SUMS.txt": (
        778913340, 192,
        "53c1266422c9222fb7ad78bb5d5460c30c6b1ca2179288be7f45f619a90bc053",
    ),
}
RELEASE_ID = 454415319
REPO = repair.update.REPO
TAG = f"v{repair.VERSION}"
ASSET_NAMES = (
    repair.SOURCE_NAME, f"{repair.PREFIX}-Repair.zip", f"{repair.PREFIX}-PUBLIC_FILES.json",
    f"{repair.PREFIX}-PROVENANCE.json", f"{repair.PREFIX}-SHA256SUMS.txt",
)


def json_bytes(value) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode()


def baseline_bytes(path: Path | None) -> bytes:
    if path is not None:
        if path.stat().st_size > repair.PROFILE_LIMIT:
            raise ValueError("Original release manifest is too large")
        data = path.read_bytes()
    else:
        with tempfile.TemporaryDirectory(prefix="magic-repair-baseline-") as temporary:
            data = repair.update.download(
                repair.update.RELEASES + TAG + "/PUBLIC_FILES.json",
                Path(temporary) / "PUBLIC_FILES.json", repair.PROFILE_LIMIT,
            )
    if repair.digest(data) != repair.BASELINE_MANIFEST_SHA256:
        raise ValueError("The original v2.4.5 manifest does not match its pinned checksum")
    repair.inventory(json.loads(data))
    return data


def reviewed_commit(root: Path) -> str:
    paths = [p.relative_to(root).as_posix() for p in build_release.public_files(root)]
    status = subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=all", "--", *paths],
        cwd=root, text=True,
    )
    if status:
        raise ValueError("Commit the reviewed public source before building a repair")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    if not re.fullmatch("[a-f0-9]{40}", commit):
        raise ValueError("Expected an exact source commit")
    return commit


def replacement(data: bytes, before: bytes, after: bytes) -> bytes:
    if data.count(before) != 1:
        raise ValueError("The reviewed updater launcher has changed; review the repair launcher")
    return data.replace(before, after)


def build(root: Path, baseline: bytes, commit: str) -> list[Path]:
    if repair.update.installed_version(root) != repair.VERSION:
        raise ValueError("This repair builder is only for application version 2.4.5")
    if repair.digest(baseline) != repair.BASELINE_MANIFEST_SHA256:
        raise ValueError("Original v2.4.5 manifest checksum mismatch")
    source = build_release.build(root)
    output = source.parent
    archive = output / repair.SOURCE_NAME
    shutil.copyfile(source, archive)
    manifest = (output / "PUBLIC_FILES.json").read_bytes()
    profile = {
        "format": "Magic-Instrumental-Desktop-Repair", "revision": repair.REVISION,
        "version": repair.VERSION, "baseline_commit": repair.BASELINE_COMMIT,
        "source_commit": commit,
        "source_url": f"https://github.com/{REPO}/commit/{commit}",
        "archive_name": archive.name, "archive_bytes": archive.stat().st_size,
        "archive_sha256": repair.digest(archive.read_bytes()),
        "manifest_sha256": repair.digest(manifest), "manifest": manifest.decode(),
        "baseline_manifest": baseline.decode(),
    }
    repair.validate_profile(profile)
    profile_data = json_bytes(profile)
    public_manifest = output / f"{repair.PREFIX}-PUBLIC_FILES.json"
    public_manifest.write_bytes(manifest)
    provenance = output / f"{repair.PREFIX}-PROVENANCE.json"
    provenance.write_bytes(profile_data)
    launcher = replacement((root / "update.command").read_bytes(),
                           b'"$UPDATER_DIR/update.py"', b'"$UPDATER_DIR/desktop_repair.py"')
    batch = replacement((root / "update.bat").read_bytes(),
                        b"update-windows.ps1", b"repair-windows.ps1")
    windows = replacement((root / "installers/update-windows.ps1").read_bytes(),
                          b"'update.py'", b"'desktop_repair.py'")
    helper_files = {
        "desktop_repair.py": (root / "tools/desktop_repair.py").read_bytes(),
        "update.py": (root / "update.py").read_bytes(),
        "app_guard.py": (root / "app_guard.py").read_bytes(),
        "repair.command": launcher, "repair.bat": batch,
        "installers/repair-windows.ps1": windows,
        "repair-profile.json": profile_data,
        "README.md": (root / "docs/DESKTOP_REPAIR.md").read_bytes(),
        "LICENSE": (root / "LICENSE").read_bytes(),
    }
    helper = output / f"{repair.PREFIX}-Repair.zip"
    with zipfile.ZipFile(helper, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for name, data in sorted(helper_files.items()):
            if Path(name).suffix in {".bat", ".ps1"}:
                data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            info = zipfile.ZipInfo(f"{repair.PREFIX}-Repair/{name}",
                                   date_time=(2026, 11, 22, 0, 0, 0))
            info.create_system = 3
            mode = 0o755 if name.endswith(".command") else 0o644
            info.external_attr = (stat.S_IFREG | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            bundle.writestr(info, data)
    files = [archive, helper, public_manifest, provenance]
    checksums = output / f"{repair.PREFIX}-SHA256SUMS.txt"
    checksums.write_text("".join(
        f"{repair.digest(path.read_bytes())}  {path.name}\n" for path in files
    ), encoding="utf-8")
    return [*files, checksums]


def gh_json(endpoint: str):
    return json.loads(subprocess.check_output(["gh", "api", endpoint], text=True))


def verify_original(release: dict, tag: dict) -> dict:
    if (
        release.get("id") != RELEASE_ID or release.get("tag_name") != TAG
        or release.get("target_commitish") != repair.BASELINE_COMMIT
        or release.get("draft") is not False or release.get("prerelease") is not False
        or tag.get("object", {}).get("type") != "commit"
        or tag["object"].get("sha") != repair.BASELINE_COMMIT
    ):
        raise ValueError("The original v2.4.5 release or tag has changed; nothing was replaced")
    assets = {asset["name"]: asset for asset in release["assets"]}
    for name, (asset_id, size, sha) in ORIGINAL_ASSETS.items():
        asset = assets.get(name, {})
        if (
            asset.get("id") != asset_id or asset.get("size") != size
            or asset.get("digest") != "sha256:" + sha or asset.get("state") != "uploaded"
        ):
            raise ValueError(f"Original release asset changed: {name}")
    return assets


def repair_notes(body: str, commit: str) -> str:
    start = f"<!-- magic-desktop-repair:{repair.REVISION}:start -->"
    end = f"<!-- magic-desktop-repair:{repair.REVISION}:end -->"
    base = repair.update.RELEASES + TAG + "/"
    section = (
        f"{start}\n## 2.4.5 자동 따라가기 수정판\n\n"
        "악보를 직접 스크롤한 뒤 자동 넘김·스크롤을 다시 켜면 현재 마디로 돌아갑니다. "
        "앱 버전은 2.4.5로 유지됩니다. 이미 2.4.5인 설치에는 자동 알림이 없으므로 "
        "아래 수정판 적용 도구를 한 번 실행하세요.\n\n"
        f"- [수정판 설치 파일]({base}{repair.SOURCE_NAME})\n"
        f"- [기존 폴더용 수정판 적용 도구]({base}{repair.PREFIX}-Repair.zip)\n"
        f"- [이 수정판의 실제 소스 commit](https://github.com/{REPO}/commit/{commit})\n\n"
        "원래 v2.4.5 태그와 최초 네 배포 자산은 그대로 보존했습니다. "
        f"이 수정판은 별도 이름 `{repair.REVISION}`으로 추가한 배포물입니다.\n{end}"
    )
    if start in body or end in body:
        if body.count(start) != 1 or body.count(end) != 1:
            raise ValueError("Duplicate or incomplete repair section in release notes")
        existing = body[body.index(start):body.index(end) + len(end)]
        if existing != section:
            raise ValueError("Conflicting repair section in release notes; keeping the body")
        return body
    if repair.REVISION in body:
        raise ValueError("Unmarked repair description already exists; keeping the release body")
    return section + "\n\n" + body


def publish(files: list[Path]):
    if len(files) != len(ASSET_NAMES) or {path.name for path in files} != set(ASSET_NAMES):
        raise ValueError("Only the five uniquely named repair assets may be uploaded")
    release_endpoint = f"repos/{REPO}/releases/tags/{TAG}"
    tag_endpoint = f"repos/{REPO}/git/ref/tags/{TAG}"
    by_name = {path.name: path for path in files}
    profile = repair.load_profile(by_name[f"{repair.PREFIX}-PROVENANCE.json"])
    source = by_name[repair.SOURCE_NAME]
    if (
        source.stat().st_size != profile["archive_bytes"]
        or repair.digest(source.read_bytes()) != profile["archive_sha256"]
        or by_name[f"{repair.PREFIX}-PUBLIC_FILES.json"].read_text(encoding="utf-8")
        != profile["manifest"]
    ):
        raise ValueError("Repair source or inventory differs from its fixed profile")
    release = gh_json(release_endpoint)
    assets = verify_original(release, gh_json(tag_endpoint))
    repair_notes(release.get("body") or "", profile["source_commit"])
    missing = []
    for path in files:
        existing = assets.get(path.name)
        if existing is None:
            missing.append(path)
        elif (
            existing.get("state") != "uploaded" or existing.get("size") != path.stat().st_size
            or existing.get("digest") != "sha256:" + repair.digest(path.read_bytes())
        ):
            raise ValueError(f"Repair revision already contains different bytes: {path.name}")
    # No deletion or --clobber: a partial upload can be resumed with identical bytes only.
    for path in missing:
        subprocess.run(["gh", "release", "upload", TAG, str(path), "--repo", REPO], check=True)
    release = gh_json(release_endpoint)
    after = verify_original(release, gh_json(tag_endpoint))
    for path in files:
        asset = after.get(path.name, {})
        if (
            asset.get("state") != "uploaded" or asset.get("size") != path.stat().st_size
            or asset.get("digest") != "sha256:" + repair.digest(path.read_bytes())
        ):
            raise ValueError(f"Uploaded repair asset failed verification: {path.name}")
    body = release.get("body") or ""
    notes = repair_notes(body, profile["source_commit"])
    if notes != body:
        if (gh_json(release_endpoint).get("body") or "") != body:
            raise ValueError("Release notes changed during publication; keeping the new body")
        with tempfile.TemporaryDirectory(prefix="magic-repair-notes-") as temporary:
            path = Path(temporary) / "notes.md"
            path.write_bytes(notes.encode())
            subprocess.run(["gh", "release", "edit", TAG, "--repo", REPO,
                            "--notes-file", str(path)], check=True)
        final = gh_json(release_endpoint)
        verify_original(final, gh_json(tag_endpoint))
        if final.get("body") != notes:
            raise ValueError("Published repair description does not match the reviewed text")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    root = build_release.ROOT
    commit = reviewed_commit(root)
    if args.publish and (
        os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("GITHUB_REF") != "refs/heads/main"
        or os.environ.get("GITHUB_SHA") != commit
    ):
        raise ValueError("Repair publication runs only in the checked main workflow")
    files = build(root, baseline_bytes(args.baseline), commit)
    if args.publish:
        publish(files)
    for path in files:
        print(path.relative_to(root))


if __name__ == "__main__":
    main()
