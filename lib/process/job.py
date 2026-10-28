import ctypes
import time
import uuid
from ctypes import wintypes

from lib.refusal import Refusal


class Basic(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class Counters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class Limits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", Basic), ("IoInfo", Counters),
        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class Accounting(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
        ("ThisPeriodTotalUserTime", ctypes.c_int64), ("ThisPeriodTotalKernelTime", ctypes.c_int64),
        ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


def kernel():
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateJobObjectW": (wintypes.HANDLE, [ctypes.c_void_p, wintypes.LPCWSTR]),
        "OpenJobObjectW": (wintypes.HANDLE, [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]),
        "SetInformationJobObject": (wintypes.BOOL, [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]),
        "QueryInformationJobObject": (wintypes.BOOL, [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]),
        "AssignProcessToJobObject": (wintypes.BOOL, [wintypes.HANDLE, wintypes.HANDLE]),
        "GetCurrentProcess": (wintypes.HANDLE, []),
        "TerminateJobObject": (wintypes.BOOL, [wintypes.HANDLE, wintypes.UINT]),
        "CloseHandle": (wintypes.BOOL, [wintypes.HANDLE]),
    }
    for name, (result, arguments) in signatures.items():
        function = getattr(api, name)
        function.restype, function.argtypes = result, arguments
    return api


def checked(value):
    if not value:
        raise ctypes.WinError(ctypes.get_last_error())
    return value


def attach(name):
    api = kernel()
    handle = checked(api.OpenJobObjectW(1, False, name))
    try:
        checked(api.AssignProcessToJobObject(handle, api.GetCurrentProcess()))
    finally:
        checked(api.CloseHandle(handle))


class Job:
    def __init__(self):
        self.name = f"Local\\wharf-suite-{uuid.uuid4().hex}"
        self.handle = None

    def __enter__(self):
        self.api = kernel()
        ctypes.set_last_error(0)
        self.handle = checked(self.api.CreateJobObjectW(None, self.name))
        try:
            if ctypes.get_last_error() == 183:
                raise Refusal("the native suite job already exists")
            limits = Limits()
            limits.BasicLimitInformation.LimitFlags = 0x2000
            checked(self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
        except Exception:
            checked(self.api.CloseHandle(self.handle))
            self.handle = None
            raise
        return self

    def active(self):
        accounting = Accounting()
        checked(self.api.QueryInformationJobObject(self.handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None))
        return accounting.ActiveProcesses

    def stop(self):
        if self.active():
            checked(self.api.TerminateJobObject(self.handle, 1))
        deadline = time.monotonic() + 5
        while self.active():
            if time.monotonic() >= deadline:
                raise Refusal("the native suite job did not become empty")
            time.sleep(0.01)

    def __exit__(self, kind, value, traceback):
        try:
            self.stop()
        except (OSError, Refusal) as failure:
            raise Refusal("native suite process-tree teardown is unverified") from failure
        finally:
            checked(self.api.CloseHandle(self.handle))
            self.handle = None
