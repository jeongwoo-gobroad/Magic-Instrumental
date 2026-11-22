# SPDX-License-Identifier: GPL-3.0-only
import hashlib
import json
import shutil
import stat
import subprocess
import sys
import zipfile
from copy import deepcopy
from pathlib import Path

import pytest

import update
from app_guard import app_guard
from tools import build_desktop_repair as builder
from tools import build_release
from tools import desktop_repair as repair


def manifest_bytes(files):
    return builder.json_bytes({
        "version": repair.VERSION,
        "files": [{"path": name, "sha256": repair.digest(data)} for name, data in files.items()],
    })


def snapshot(root):
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*")
            if path.is_file() and ".magic-updates" not in path.parts
            and path.name != ".magic-update.lock"}


@pytest.fixture
def edition(tmp_path, monkeypatch):
    original = {
        "main.py": b"original entry\n", "bootstrap.py": b"original setup\n",
        "practice_player/__init__.py": b'__version__ = "2.4.5"\n',
        "practice_player/score_gui.py": b"original follow behavior\n",
        "targets/.gitkeep": b"",
    }
    fixed = {**original, "practice_player/score_gui.py": b"restored follow behavior\n",
             "tools/desktop_repair.py": b"authored repair fixture\n"}
    root = tmp_path / "Existing program 한글"
    for name, data in original.items():
        if name.startswith("targets/"):
            continue
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    for name in ("targets/original.wav", "score_targets/score.pdf", "processed/reviewed.json",
                 "models/weights.bin", "venv-location.windows.txt", "my-export.magic"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"authored private sentinel")
    archive = tmp_path / repair.SOURCE_NAME
    with zipfile.ZipFile(archive, "w") as bundle:
        for name, data in fixed.items():
            info = zipfile.ZipInfo(f"Magic-Instrumental-{repair.VERSION}/{name}")
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            bundle.writestr(info, data)
    baseline, target = manifest_bytes(original), manifest_bytes(fixed)
    monkeypatch.setattr(repair, "BASELINE_MANIFEST_SHA256", repair.digest(baseline))
    profile = {
        "format": "Magic-Instrumental-Desktop-Repair", "revision": repair.REVISION,
        "version": repair.VERSION, "baseline_commit": repair.BASELINE_COMMIT,
        "source_commit": "a" * 40,
        "source_url": f"https://github.com/{update.REPO}/commit/" + "a" * 40,
        "archive_name": repair.SOURCE_NAME, "archive_bytes": archive.stat().st_size,
        "archive_sha256": repair.digest(archive.read_bytes()), "manifest": target.decode(),
        "manifest_sha256": repair.digest(target), "baseline_manifest": baseline.decode(),
    }
    downloads = []

    def download(url, path, limit):
        assert url == repair.SOURCE_URL and limit == profile["archive_bytes"]
        data = archive.read_bytes()
        path.write_bytes(data)
        downloads.append(url)
        return data

    monkeypatch.setattr(update, "download", download)
    monkeypatch.setattr(update, "check_legacy_processes", lambda _: None)
    return root, profile, archive, downloads, fixed


@pytest.mark.parametrize("current,mixed", [("2.4.5", False), ("2.4.5", True), ("2.4.4", False)])
def test_repair_preserves_private_files_and_updates_in_place(edition, current, mixed):
    root, profile, _, downloads, fixed = edition
    # Official Python files use LF; fixture hashes must stay identical on Windows.
    (root / "practice_player/__init__.py").write_bytes(
        f'__version__ = "{current}"\n'.encode("utf-8")
    )
    if mixed:
        (root / "practice_player/score_gui.py").write_bytes(fixed["practice_player/score_gui.py"])
    before = snapshot(root)
    installs = []
    assert repair.repair(root, profile, installer=installs.append)
    assert downloads == [repair.SOURCE_URL] and installs == [root]
    for name, data in before.items():
        if name not in fixed:
            assert (root / name).read_bytes() == data
    assert update.installed_version(root) == "2.4.5"
    assert (root / "practice_player/score_gui.py").read_bytes() == fixed[
        "practice_player/score_gui.py"
    ]
    assert not (root / "targets/.gitkeep").exists()
    assert not (root / ".magic-updates/pending.json").exists()
    assert next((root / ".magic-updates").glob("*/backup/main.py")).read_bytes() == before[
        "main.py"
    ]


def test_already_applied_is_an_offline_noop(edition):
    root, profile, _, downloads, fixed = edition
    for name, data in fixed.items():
        if name.startswith("targets/"):
            continue
        path = root / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(data)
    before = snapshot(root)
    assert not repair.repair(root, profile, installer=lambda _: pytest.fail("unneeded setup"))
    assert not downloads and snapshot(root) == before


@pytest.mark.parametrize("change", ["future-version", "later-same-version", "missing-file"])
def test_future_or_unknown_managed_code_is_preserved(edition, change):
    root, profile, _, downloads, _ = edition
    if change == "future-version":
        (root / "practice_player/__init__.py").write_bytes(b'__version__ = "2.4.6"\n')
    elif change == "later-same-version":
        (root / "practice_player/score_gui.py").write_bytes(b"later edition or a user edit")
    else:
        (root / "practice_player/score_gui.py").unlink()
    before = snapshot(root)
    with pytest.raises(ValueError):
        repair.repair(root, profile, installer=lambda _: pytest.fail("unexpected setup"))
    assert not downloads and snapshot(root) == before


def test_active_application_prevents_repair(edition):
    root, profile, _, downloads, _ = edition
    before = snapshot(root)
    with app_guard(root), pytest.raises(RuntimeError):
        repair.repair(root, profile)
    assert not downloads and snapshot(root) == before


def test_failed_setup_rolls_back_same_version_code(edition):
    root, profile, _, _, _ = edition
    before, installs = snapshot(root), []

    def setup(path):
        installs.append(path)
        if len(installs) == 1:
            raise RuntimeError("authored setup failure")

    with pytest.raises(RuntimeError, match="authored setup failure"):
        repair.repair(root, profile, installer=setup)
    assert installs == [root, root] and snapshot(root) == before
    assert not (root / ".magic-updates/pending.json").exists()


def test_corrupt_download_never_changes_program(edition):
    root, profile, archive, _, _ = edition
    before = snapshot(root)
    archive.write_bytes(archive.read_bytes()[:-1] + b"x")
    with pytest.raises(ValueError, match="ZIP 검증"):
        repair.repair(root, profile)
    assert snapshot(root) == before


def test_manual_change_during_download_is_not_overwritten(edition, monkeypatch):
    root, profile, _, _, _ = edition
    original_download = update.download

    def download(*args):
        data = original_download(*args)
        (root / "practice_player/score_gui.py").write_bytes(b"concurrent manual edit")
        return data

    monkeypatch.setattr(update, "download", download)
    with pytest.raises(ValueError, match="다른 프로그램 파일"):
        repair.repair(root, profile)
    assert (root / "practice_player/score_gui.py").read_bytes() == b"concurrent manual edit"
    assert not (root / ".magic-updates/pending.json").exists()


def test_older_install_replaced_during_download_is_not_downgraded(edition, monkeypatch):
    root, profile, _, _, _ = edition
    version_file = root / "practice_player/__init__.py"
    version_file.write_bytes(b'__version__ = "2.4.4"\n')
    original_download = update.download

    def download(*args):
        data = original_download(*args)
        version_file.write_bytes(b'__version__ = "2.4.6"\n')
        return data

    monkeypatch.setattr(update, "download", download)
    with pytest.raises(ValueError, match="버전이 바뀌었습니다"):
        repair.repair(root, profile)
    assert update.installed_version(root) == "2.4.6"
    assert (root / "practice_player/score_gui.py").read_bytes() == b"original follow behavior\n"
    assert not (root / ".magic-updates/pending.json").exists()


def test_profile_rejects_changed_original_manifest_and_target_inventory(edition):
    _, profile, _, _, _ = edition
    changed = {**profile, "baseline_manifest": profile["baseline_manifest"] + " "}
    with pytest.raises(ValueError, match="원본 v2.4.5"):
        repair.validate_profile(changed)
    target = json.loads(profile["manifest"])
    target["files"].append({"path": "processed/reviewed.json", "sha256": "0" * 64})
    data = builder.json_bytes(target)
    changed = {**profile, "manifest": data.decode(), "manifest_sha256": repair.digest(data)}
    with pytest.raises(ValueError, match="허용되지 않은"):
        repair.validate_profile(changed)


def test_packaged_helper_is_reproducible_and_compatible_with_existing_updater(tmp_path,
                                                                           monkeypatch):
    root = tmp_path / "source"
    for source in build_release.public_files():
        path = root / source.relative_to(build_release.ROOT)
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, path)
    (root / "practice_player/__init__.py").write_bytes(
        b'# SPDX-License-Identifier: GPL-3.0-only\n__version__ = "2.4.5"\n'
    )
    build_release.build(root)
    baseline = (root / "dist/PUBLIC_FILES.json").read_bytes()
    monkeypatch.setattr(repair, "BASELINE_MANIFEST_SHA256", repair.digest(baseline))
    changed = root / "practice_player/score_gui.py"
    changed.write_bytes(changed.read_bytes() + b"\n# Authored repair packaging probe.\n")
    files = builder.build(root, baseline, "a" * 40)
    first = {p.name: p.read_bytes() for p in files}
    assert {p.name for p in files} == set(builder.ASSET_NAMES)
    assert {p.name: p.read_bytes() for p in builder.build(root, baseline, "a" * 40)} == first
    target = json.loads(first[f"{repair.PREFIX}-PUBLIC_FILES.json"])
    assert all(update.public_path(row["path"]) for row in target["files"])
    stage = tmp_path / "staged"
    update.stage_release(root / "dist" / repair.SOURCE_NAME, target, stage)
    assert update.installed_version(stage) == "2.4.5"
    with zipfile.ZipFile(root / "dist" / f"{repair.PREFIX}-Repair.zip") as helper:
        prefix = f"{repair.PREFIX}-Repair/"
        profile = json.loads(helper.read(prefix + "repair-profile.json"))
        assert profile["archive_sha256"] == repair.digest(first[repair.SOURCE_NAME])
        assert profile["source_commit"] == "a" * 40
        assert helper.getinfo(prefix + "repair.command").external_attr >> 16 & stat.S_IXUSR
        assert b"desktop_repair.py" in helper.read(prefix + "repair.command")
        assert b"desktop_repair.py" in helper.read(prefix + "installers/repair-windows.ps1")
        assert b"repair-windows.ps1" in helper.read(prefix + "repair.bat")
        helper.extractall(tmp_path / "helper")
    # A real separate interpreter proves that the ZIP carries its updater imports.
    command = tmp_path / "helper" / prefix / "desktop_repair.py"
    subprocess.run([sys.executable, str(command), "--help"], check=True, capture_output=True)
    checksums = first[f"{repair.PREFIX}-SHA256SUMS.txt"].decode()
    for name, data in first.items():
        if not name.endswith("-SHA256SUMS.txt"):
            assert hashlib.sha256(data).hexdigest() + "  " + name in checksums


def original_release():
    return {
        "id": builder.RELEASE_ID, "tag_name": builder.TAG,
        "target_commitish": repair.BASELINE_COMMIT, "draft": False, "prerelease": False,
        "body": "Original release notes.\n\nPreserve this text exactly.\n",
        "assets": [{"name": name, "id": asset_id, "size": size, "state": "uploaded",
                    "digest": "sha256:" + sha}
                   for name, (asset_id, size, sha) in builder.ORIGINAL_ASSETS.items()],
    }


@pytest.mark.parametrize("existing_count", [0, 2])
def test_additive_publish_preserves_original_and_resumes_identical_assets(edition, tmp_path,
                                                                       monkeypatch, existing_count):
    _, profile, source, _, _ = edition
    release = original_release()
    original = deepcopy(release["assets"])
    original_body = release["body"]
    files, uploads, edits = [], [], []
    for name in builder.ASSET_NAMES:
        path = tmp_path / name
        if path != source:
            data = b"authored publication fixture " + name.encode()
            if name.endswith("-PROVENANCE.json"):
                data = builder.json_bytes(profile)
            elif name.endswith("-PUBLIC_FILES.json"):
                data = profile["manifest"].encode()
            path.write_bytes(data)
        files.append(path)
    for path in files[:existing_count]:
        release["assets"].append({"name": path.name, "state": "uploaded",
                                  "size": path.stat().st_size,
                                  "digest": "sha256:" + repair.digest(path.read_bytes())})

    def gh(endpoint):
        return deepcopy(release) if "/releases/" in endpoint else {
            "object": {"type": "commit", "sha": repair.BASELINE_COMMIT},
        }

    def upload(args, **kwargs):
        if args[:4] == ["gh", "release", "edit", builder.TAG]:
            assert args[4:7] == ["--repo", builder.REPO, "--notes-file"]
            release["body"] = Path(args[7]).read_bytes().decode()
            edits.append(release["body"])
            return
        assert args[:4] == ["gh", "release", "upload", builder.TAG]
        assert "--clobber" not in args
        path = Path(args[4])
        uploads.append(path.name)
        release["assets"].append({"name": path.name, "state": "uploaded",
                                  "size": path.stat().st_size,
                                  "digest": "sha256:" + repair.digest(path.read_bytes())})

    monkeypatch.setattr(builder, "gh_json", gh)
    monkeypatch.setattr(builder.subprocess, "run", upload)
    builder.publish(files)
    assert uploads == list(builder.ASSET_NAMES[existing_count:])
    assert release["assets"][:4] == original
    assert len(edits) == 1 and release["body"].endswith(original_body)
    assert f"/commit/{profile['source_commit']}" in release["body"]
    builder.publish(files)
    assert len(uploads) == 5 - existing_count and len(edits) == 1
    release["assets"][-1]["digest"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError, match="different bytes"):
        builder.publish(files)
    assert len(uploads) == 5 - existing_count and release["assets"][:4] == original


def test_notes_preserve_original_text_and_reject_revision_conflicts():
    original = "Original notes\r\n\r\nKeep these line endings.\n"
    notes = builder.repair_notes(original, "a" * 40)
    assert notes.endswith(original)
    assert builder.repair_notes(notes, "a" * 40) == notes
    for conflict in (notes, notes + notes, f"Existing {repair.REVISION} description"):
        with pytest.raises(ValueError, match="repair"):
            builder.repair_notes(conflict, "b" * 40)


@pytest.mark.parametrize("change", ["tag", "asset-id", "asset-hash"])
def test_changed_original_release_refuses_publication(change):
    release = original_release()
    tag = {"object": {"type": "commit", "sha": repair.BASELINE_COMMIT}}
    if change == "tag":
        tag["object"]["sha"] = "b" * 40
    elif change == "asset-id":
        release["assets"][0]["id"] += 1
    else:
        release["assets"][0]["digest"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError, match="changed"):
        builder.verify_original(release, tag)
