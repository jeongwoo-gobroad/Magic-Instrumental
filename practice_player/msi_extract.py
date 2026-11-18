# SPDX-License-Identifier: GPL-3.0-only
"""Read a verified MSI as a database/CAB archive, without running installer actions.

Windows' built-in read-only MSI API and expand.exe need no elevation, registry
registration, installer service or third-party extractor. Only the pinned
Audiveris layout (embedded CABs, ordinary files) is supported.
"""

import ctypes
import os
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path


def safe_name(value: str) -> str:
    # MSI short|long naming; target:source directory naming is handled by caller.
    name = value.split("|")[-1]
    if not name or name in {".", ".."} or any(c in name for c in '/\\:'):
        raise ValueError(f"Unsafe MSI path component: {value!r}")
    return name


def file_layout(files: list, components: list, directories: list) -> dict[str, Path]:
    """Keep the complete Audiveris subtree, excluding machine-specific parent dirs."""
    dirs = {key: (parent, name.split(":")[0]) for key, parent, name in directories}
    roots = [key for key, (_, name) in dirs.items() if name.split("|")[-1] == "Audiveris"]
    if len(roots) != 1:
        raise ValueError("Expected one Audiveris directory in official MSI")
    components = dict(components)

    def relative(key: str, seen: set) -> Path:
        if key == roots[0]:
            return Path("Audiveris")
        if key in seen or key not in dirs:
            raise ValueError("Invalid MSI directory tree")
        parent, name = dirs[key]
        base = relative(parent, seen | {key})
        return base if name == "." else base / safe_name(name)

    result, used = {}, set()
    for key, component, name in files:
        target = relative(components[component], set()) / safe_name(name)
        folded = str(target).casefold()
        if folded in used:
            raise ValueError("Duplicate MSI destination")
        used.add(folded)
        result[safe_name(key)] = target
    return result


class Database:
    def __init__(self, path: Path):
        from ctypes import wintypes as w

        self.dll = ctypes.WinDLL("msi.dll")
        # MSIHANDLE is a 32-bit unsigned integer, including on Windows x64.
        signatures = {
            "MsiOpenDatabaseW": [w.LPCWSTR, w.LPCWSTR, ctypes.POINTER(w.UINT)],
            "MsiDatabaseOpenViewW": [w.UINT, w.LPCWSTR, ctypes.POINTER(w.UINT)],
            "MsiViewExecute": [w.UINT, w.UINT],
            "MsiViewFetch": [w.UINT, ctypes.POINTER(w.UINT)],
            "MsiRecordGetStringW": [w.UINT, w.UINT, w.LPWSTR, ctypes.POINTER(w.DWORD)],
            "MsiRecordReadStream": [w.UINT, w.UINT, ctypes.c_void_p, ctypes.POINTER(w.DWORD)],
            "MsiCloseHandle": [w.UINT],
        }
        for name, args in signatures.items():
            function = getattr(self.dll, name)
            function.argtypes, function.restype = args, w.UINT
        self.handle = w.UINT()
        self.check(self.dll.MsiOpenDatabaseW(str(path), None, ctypes.byref(self.handle)))

    @staticmethod
    def check(code: int):
        if code:
            raise OSError(f"MSI archive read failed (Windows error {code})")

    def close(self):
        self.dll.MsiCloseHandle(self.handle)

    @contextmanager
    def view(self, query: str):
        handle = ctypes.c_uint()
        self.check(self.dll.MsiDatabaseOpenViewW(self.handle, query, ctypes.byref(handle)))
        try:
            self.check(self.dll.MsiViewExecute(handle, 0))
            yield handle
        finally:
            self.dll.MsiCloseHandle(handle)

    def rows(self, query: str, columns: int) -> list:
        values = []
        with self.view(query) as view:
            while True:
                record = ctypes.c_uint()
                code = self.dll.MsiViewFetch(view, ctypes.byref(record))
                if code == 259:  # ERROR_NO_MORE_ITEMS
                    break
                self.check(code)
                try:
                    row = []
                    for index in range(1, columns + 1):
                        buffer, length = ctypes.create_unicode_buffer(32768), ctypes.c_ulong(32768)
                        self.check(self.dll.MsiRecordGetStringW(
                            record, index, buffer, ctypes.byref(length),
                        ))
                        row.append(buffer.value)
                    values.append(row)
                finally:
                    self.dll.MsiCloseHandle(record)
        return values

    def stream(self, name: str, path: Path):
        # Names originate in the checksum-verified MSI, still escape the SQL literal.
        escaped = name.replace("'", "''")
        with self.view(f"SELECT `Data` FROM `_Streams` WHERE `Name`='{escaped}'") as view:
            record = ctypes.c_uint()
            self.check(self.dll.MsiViewFetch(view, ctypes.byref(record)))
            try:
                with path.open("wb") as output:
                    while True:
                        buffer, size = ctypes.create_string_buffer(65536), ctypes.c_ulong(65536)
                        self.check(self.dll.MsiRecordReadStream(
                            record, 1, buffer, ctypes.byref(size),
                        ))
                        if not size.value:
                            break
                        output.write(buffer.raw[:size.value])
            finally:
                self.dll.MsiCloseHandle(record)


def extract_msi(archive: Path, destination: Path):
    database = Database(archive)
    scratch = destination / "cabinets"
    scratch.mkdir()
    try:
        layout = file_layout(
            database.rows("SELECT `File`, `Component_`, `FileName` FROM `File`", 3),
            database.rows("SELECT `Component`, `Directory_` FROM `Component`", 2),
            database.rows(
                "SELECT `Directory`, `Directory_Parent`, `DefaultDir` FROM `Directory`", 3,
            ),
        )
        cabinets = database.rows("SELECT `Cabinet` FROM `Media`", 1)
        expand = Path(os.environ["SystemRoot"]) / "System32/expand.exe"
        for index, (cabinet,) in enumerate(cabinets):
            if not cabinet.startswith("#"):
                raise ValueError("Expected embedded Audiveris MSI cabinet")
            cab = scratch / f"{index}.cab"
            database.stream(cabinet[1:], cab)
            subprocess.run([str(expand), "-F:*", str(cab), str(scratch)], check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
            cab.unlink()
        for name, relative in layout.items():
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(scratch / name), target)
    finally:
        database.close()
        shutil.rmtree(scratch, ignore_errors=True)
