"""Windows 下的进程内存与进程树（ctypes 调 kernel32，不依赖第三方包）。

Windows venv 的 Scripts\\python.exe 通常是启动器：它再拉起真正的解释器，并让后者的 sys.executable 仍指向启动器。
所以这种 venv 下 Worker 拉起的 Runner 父进程是启动器，Worker 是祖父进程；判断依据是父进程映像与本进程的
sys.executable 是同一个文件、却不是本进程自己的映像。

Windows 不给孤儿进程改父进程号，进程表里的父 pid 可能早已退出并被复用，所以认后代时要求子进程不早于父进程创建。
"""

from __future__ import annotations

from ctypes import wintypes
from typing import Callable, Dict, List, Optional, Tuple

import ctypes
import os
import sys

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_VM_READ = 0x0010
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
MAX_PATH_CHARS = 32768


class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


def _kernel32() -> ctypes.WinDLL:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.K32GetProcessMemoryInfo.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX), wintypes.DWORD)
    kernel32.GetProcessHandleCount.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    kernel32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
    kernel32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
    return kernel32


def _open(kernel32: ctypes.WinDLL, pid: int) -> wintypes.HANDLE:
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


def process_memory(pid: int) -> Dict[str, int]:
    """工作集、峰值工作集、提交量（PrivateUsage，Windows 下最接近 Linux 匿名内存的指标）与句柄数，单位字节。"""
    kernel32 = _kernel32()
    handle = _open(kernel32, pid)
    try:
        counters = PROCESS_MEMORY_COUNTERS_EX()
        counters.cb = ctypes.sizeof(counters)
        if not kernel32.K32GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        handles = wintypes.DWORD()
        if not kernel32.GetProcessHandleCount(handle, ctypes.byref(handles)):
            raise ctypes.WinError(ctypes.get_last_error())
        return {
            "working_set": counters.WorkingSetSize,
            "peak_working_set": counters.PeakWorkingSetSize,
            "private_usage": counters.PrivateUsage,
            "pagefile_usage": counters.PagefileUsage,
            "peak_pagefile_usage": counters.PeakPagefileUsage,
            "handle_count": handles.value,
        }
    finally:
        kernel32.CloseHandle(handle)


def process_created(pid: int) -> int:
    """创建时间（FILETIME，100 纳秒）；pid 会被复用，与 pid 合起来才能确认还是同一个进程。"""
    kernel32 = _kernel32()
    handle = _open(kernel32, pid)
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel32.GetProcessTimes(handle, *(ctypes.byref(item) for item in times)):
            raise ctypes.WinError(ctypes.get_last_error())
        return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    finally:
        kernel32.CloseHandle(handle)


def process_image(pid: int) -> str:
    """映像完整路径。只用于判断启动器，不写进样本：路径里常带系统用户名。"""
    kernel32 = _kernel32()
    handle = _open(kernel32, pid)
    try:
        size = wintypes.DWORD(MAX_PATH_CHARS)
        buffer = ctypes.create_unicode_buffer(MAX_PATH_CHARS)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        return buffer.value
    finally:
        kernel32.CloseHandle(handle)


def process_table() -> Dict[int, Tuple[int, str, int]]:
    """{pid: (ppid, 可执行文件名, 线程数)}。"""
    kernel32 = _kernel32()
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        table: Dict[int, Tuple[int, str, int]] = {}
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            table[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile, entry.cntThreads)
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        return table
    finally:
        kernel32.CloseHandle(snapshot)


def resolve_worker(table: Dict[int, Tuple[int, str, int]], self_pid: int, is_launcher: Callable[[int], bool]) -> int:
    """按进程表找 Worker：父进程；父进程若是 venv 启动器则再上移一层。"""
    parent = table[self_pid][0]
    if is_launcher(parent):
        parent = table[parent][0]
        if is_launcher(parent):
            raise RuntimeError("连续两层都是 venv 启动器，进程树形状不符，无法确定 Worker")
    return parent


def is_venv_launcher(image: str, executable: str, own_image: str) -> bool:
    """image 是否是 venv 的启动器。

    宿主每一层都用 sys.executable 拉起下一层，所以每层进程的“启动命令”都是同一个文件。有启动器时，
    真正运行的解释器（包括本进程）映像是另一个文件；没有启动器时（系统或嵌入式 Python、--symlinks 建的 venv、
    Scripts 里放的是解释器副本），每层的映像就是本进程自己的映像。不拿 sys._base_executable 比：
    解释器副本式的 venv 里它指向安装目录，与真正运行的映像不是同一个文件。
    按文件身份比较：安装路径经过目录联接或符号链接时，QueryFullProcessImageNameW 给出的是解析后的真实路径。
    """
    return os.path.samefile(image, executable) and not os.path.samefile(image, own_image)


def find_worker_pid(self_pid: Optional[int] = None) -> int:
    self_pid = os.getpid() if self_pid is None else self_pid
    executable, own_image = sys.executable, process_image(self_pid)
    return resolve_worker(
        process_table(),
        self_pid,
        lambda pid: is_venv_launcher(process_image(pid), executable, own_image),
    )


def descendants(root_pid: int) -> Dict[int, str]:
    """root_pid 的全部后代 {pid: 可执行文件名}。

    只承认创建时间不早于父进程的子进程：父 pid 被复用时，原父进程留下的孤儿会挂在新进程名下，进程表里甚至
    会出现环。打不开的进程（已退出或无权访问）既无法核实父子关系，也读不到内存，直接略过。
    """
    table = process_table()
    children: Dict[int, List[int]] = {}
    for pid, (ppid, _, _) in table.items():
        if pid != ppid:
            children.setdefault(ppid, []).append(pid)
    created = {root_pid: process_created(root_pid)}
    result: Dict[int, str] = {}
    frontier = [root_pid]
    while frontier:
        parent = frontier.pop()
        for pid in children.get(parent, []):
            if pid in created:
                continue
            try:
                child_created = process_created(pid)
            except OSError:
                continue
            if child_created < created[parent]:
                continue
            created[pid] = child_created
            result[pid] = table[pid][1]
            frontier.append(pid)
    return result
