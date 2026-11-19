# SPDX-License-Identifier: GPL-3.0-only
"""Cross-process shared app / exclusive update lock, without third-party imports."""

import os
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def app_guard(root: Path, *, updating=False):
    lock = root / ".magic-update.lock"
    if lock.is_symlink():
        raise RuntimeError("업데이트 잠금 파일이 링크입니다. 일반 설치 폴더를 사용하세요.")
    with lock.open("a+b") as stream:
        if os.fstat(stream.fileno()).st_size == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        import sys

        if sys.platform == "win32":
            import ctypes
            import msvcrt
            from ctypes import wintypes

            class Overlapped(ctypes.Structure):
                _fields_ = [
                    ("Internal", ctypes.c_size_t),
                    ("InternalHigh", ctypes.c_size_t),
                    ("Offset", wintypes.DWORD),
                    ("OffsetHigh", wintypes.DWORD),
                    ("hEvent", wintypes.HANDLE),
                ]

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.LockFileEx.argtypes = [
                wintypes.HANDLE,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.DWORD,
                ctypes.POINTER(Overlapped),
            ]
            kernel.UnlockFileEx.argtypes = [
                wintypes.HANDLE,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.DWORD,
                ctypes.POINTER(Overlapped),
            ]
            handle = msvcrt.get_osfhandle(stream.fileno())
            overlapped = Overlapped()
            if not kernel.LockFileEx(
                handle, 1 | (2 if updating else 0), 0, 1, 0, ctypes.byref(overlapped)
            ):
                raise RuntimeError("프로그램·설치·다른 업데이트 창을 닫은 뒤 다시 실행하세요.")

            def unlock():
                kernel.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlapped))
        else:
            import fcntl

            try:
                fcntl.flock(stream, (fcntl.LOCK_EX if updating else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError(
                    "프로그램·설치·다른 업데이트 창을 닫은 뒤 다시 실행하세요."
                ) from error

            def unlock():
                fcntl.flock(stream, fcntl.LOCK_UN)

        try:
            if not updating and (root / ".magic-updates/pending.json").exists():
                raise RuntimeError("중단된 업데이트가 있습니다. update 파일을 다시 더블클릭하세요.")
            yield
        finally:
            unlock()
