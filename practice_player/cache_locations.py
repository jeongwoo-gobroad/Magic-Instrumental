# SPDX-License-Identifier: GPL-3.0-only
"""Persist cache destinations without relocating existing audio or reviewed scores."""

import json
import os
import tempfile
from hashlib import sha256
from pathlib import Path

SETTINGS_NAME = "cache-locations.json"
_MAX_SETTINGS_BYTES = 16 * 1024


def linked_directory(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _settings_revision(settings: Path):
    try:
        with settings.open("rb") as source:
            content = source.read(_MAX_SETTINGS_BYTES + 1)
            info = os.fstat(source.fileno())
        return info.st_size, info.st_mtime_ns, sha256(content).hexdigest()
    except FileNotFoundError:
        return None


def read_cache_locations(root: Path) -> tuple[Path, Path, str, tuple | None]:
    default = root / "processed"
    settings = default / SETTINGS_NAME
    audio = score = default
    revision = None
    try:
        if settings.is_symlink():
            raise ValueError("저장 위치 설정 파일에 바로가기를 사용할 수 없습니다.")
        with settings.open("rb") as source:
            content = source.read(_MAX_SETTINGS_BYTES + 1)
            info = os.fstat(source.fileno())
        revision = info.st_size, info.st_mtime_ns, sha256(content).hexdigest()
        if len(content) > _MAX_SETTINGS_BYTES:
            raise ValueError("저장 위치 설정 파일이 너무 큽니다.")
        data = json.loads(content)
        if (not isinstance(data, dict) or type(data.get("version")) is not int
                or data["version"] != 1
                or set(data) != {"version", "audio", "score"}):
            raise ValueError("저장 위치 설정 형식이 올바르지 않습니다.")
        locations = []
        for name in ("audio", "score"):
            value = data[name]
            if value is None:
                locations.append(default)
            elif (isinstance(value, str) and value and "\0" not in value
                    and Path(value).is_absolute()):
                locations.append(Path(value))
            else:
                raise ValueError("저장 위치는 전체 폴더 경로여야 합니다.")
        return locations[0], locations[1], "", revision
    except FileNotFoundError:
        return audio, score, "", revision
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError) as error:
        message = (
            "캐시 저장 위치 설정을 읽을 수 없습니다. 상세 옵션에서 저장 위치를 다시 저장하세요. "
            f"({error})"
        )
        return audio, score, message, revision


def ensure_cache_directory(path: Path) -> Path:
    """Check the selected root before a worker creates its managed descendants."""
    if linked_directory(path):
        raise ValueError("캐시 폴더의 바로가기·심볼릭 링크는 사용할 수 없습니다.")
    if path.exists() and not path.is_dir():
        raise ValueError("캐시 저장 위치는 파일이 아닌 폴더여야 합니다.")
    path.mkdir(parents=True, exist_ok=True)
    return path


def require_current_locations(paths) -> None:
    if paths.cache_location_error:
        raise ValueError(paths.cache_location_error)
    try:
        current = _settings_revision(paths.processed / SETTINGS_NAME)
    except OSError as error:
        raise ValueError(
            "캐시 저장 위치 설정을 읽을 수 없습니다. 상세 옵션에서 확인하세요."
        ) from error
    if current != paths.cache_location_revision:
        raise ValueError("다른 창에서 저장 위치를 변경했습니다. 프로그램을 다시 열어 확인하세요.")


def _writable_location(value, label: str, reserved: tuple[Path, ...]) -> Path:
    if not isinstance(value, (str, os.PathLike)) or not str(value).strip():
        raise ValueError(f"{label} 폴더를 선택하거나 전체 경로를 입력하세요.")
    try:
        path = Path(value).expanduser()
        if not path.is_absolute() or "\0" in str(path):
            raise ValueError("전체 폴더 경로를 입력하세요.")
        if linked_directory(path):
            raise ValueError("바로가기·심볼릭 링크 대신 실제 폴더를 선택하세요.")
        # 상위 폴더의 운영체제 별칭은 실제 경로로 저장한다.
        path = path.resolve()
        if any(path == filename or filename in path.parents for filename in reserved):
            raise ValueError("프로그램 설정 파일의 경로는 저장 폴더로 사용할 수 없습니다.")
        ensure_cache_directory(path)
        with tempfile.TemporaryFile(prefix=".magic-write-check-", dir=path) as probe:
            probe.write(b"Magic-Instrumental cache location check\n")
            probe.flush()
            os.fsync(probe.fileno())
    except (OSError, ValueError, RuntimeError) as error:
        raise ValueError(
            f"{label}에 저장할 수 없습니다. 다른 폴더를 선택하세요. ({error})"
        ) from error
    return path


def save_cache_locations(paths, audio_folder, score_folder):
    """Validate both roots, atomically save them, then return a fresh Paths snapshot."""
    from filelock import FileLock

    from .config import Paths

    default = paths.processed.resolve()
    reserved = tuple(default / name for name in (
        SETTINGS_NAME, ".cache-locations.lock", "ui.ini", "magic-export-request.json",
    ))
    audio = _writable_location(audio_folder, "음원 캐시 폴더", reserved)
    score = _writable_location(score_folder, "악보 분석 캐시 폴더", reserved)
    settings = paths.processed / SETTINGS_NAME
    ensure_cache_directory(paths.processed)
    if settings.is_symlink() or (settings.exists() and not settings.is_file()):
        raise ValueError(
            "저장 위치 설정 파일이 올바르지 않습니다. 기존 설정은 변경하지 않았습니다."
        )
    data = {
        "version": 1,
        "audio": None if audio == default else str(audio),
        "score": None if score == default else str(score),
    }
    lock = paths.processed / ".cache-locations.lock"
    if lock.is_symlink():
        raise ValueError("저장 위치 설정 잠금 파일에 바로가기를 사용할 수 없습니다.")
    with FileLock(str(lock), timeout=5):
        temporary = None
        try:
            if _settings_revision(settings) != paths.cache_location_revision:
                raise ValueError(
                    "다른 창에서 저장 위치를 변경했습니다. 프로그램을 다시 열어 확인하세요."
                )
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", prefix=".cache-locations-", suffix=".json",
                dir=paths.processed, delete=False,
            ) as output:
                temporary = Path(output.name)
                json.dump(data, output, ensure_ascii=False, indent=2, allow_nan=False)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, settings)
        except OSError as error:
            raise ValueError(
                f"저장 위치 설정을 저장하지 못했습니다. 기존 위치를 유지합니다. ({error})"
            ) from error
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return Paths(paths.root)
