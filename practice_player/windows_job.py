# SPDX-License-Identifier: GPL-3.0-only
"""Keep a GUI worker and its native child processes in one Windows job."""

import ctypes
import uuid
from ctypes import wintypes


class BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", BasicLimits), ("IoInfo", ctypes.c_uint64 * 6),
        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


kernel = ctypes.WinDLL("kernel32", use_last_error=True)


def api(name, result, *arguments):
    function = getattr(kernel, name)
    function.restype = result
    function.argtypes = arguments
    return function


create = api("CreateJobObjectW", wintypes.HANDLE, ctypes.c_void_p, wintypes.LPCWSTR)
configure = api("SetInformationJobObject", wintypes.BOOL, wintypes.HANDLE, ctypes.c_int,
                ctypes.POINTER(ExtendedLimits), wintypes.DWORD)
open_job = api("OpenJobObjectW", wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
assign = api("AssignProcessToJobObject", wintypes.BOOL, wintypes.HANDLE, wintypes.HANDLE)
current_process = api("GetCurrentProcess", wintypes.HANDLE)
close_handle = api("CloseHandle", wintypes.BOOL, wintypes.HANDLE)


class WorkerJob:
    def __init__(self):
        self.name = "Local\\Magic-Instrumental-" + uuid.uuid4().hex
        self.handle = create(None, self.name)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not configure(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def close(self):
        if self.handle:
            close_handle(self.handle)
            self.handle = None


def join_worker_job(name):
    # Enrol the real interpreter before it starts FFmpeg/Java. A venv launcher
    # can itself be a separate process on Windows.
    handle = open_job(1, False, name)  # JOB_OBJECT_ASSIGN_PROCESS
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not assign(handle, current_process()):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        close_handle(handle)
