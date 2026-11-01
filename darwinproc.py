"""macOS 下的进程内存与进程树（ctypes 调 libproc，不依赖第三方包）。

venv 的 python 是符号链接，框架版 Python 的 bin/python 用 exec 替换自身、pid 不变，所以 Runner 的父进程就是 Worker。
孤儿进程的父进程号立刻变成 launchd，不会出现 Windows 上父 pid 被复用的情况，认后代不需要比较创建时间。

结构体布局来自 <sys/proc_info.h> 与 <sys/resource.h>；Apple 不保证 libproc 的 ABI，测试在 macOS 上用 cc 对照系统头文件。
"""

from __future__ import annotations

from typing import Dict, List

import ctypes
import errno
import os

LIBPROC_PATH = "/usr/lib/libproc.dylib"
PROC_PIDTBSDINFO = 3
PROC_PIDTASKINFO = 4
PROC_PPID_ONLY = 6
RUSAGE_INFO_V4 = 4
MAXCOMLEN = 16
# proc_listpids 先问大小再取，两次调用之间可能又有新进程。
PID_LIST_SLACK = 64


class proc_bsdinfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * MAXCOMLEN),
        ("pbi_name", ctypes.c_char * (2 * MAXCOMLEN)),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


class proc_taskinfo(ctypes.Structure):
    _fields_ = [
        ("pti_virtual_size", ctypes.c_uint64),
        ("pti_resident_size", ctypes.c_uint64),
        ("pti_total_user", ctypes.c_uint64),
        ("pti_total_system", ctypes.c_uint64),
        ("pti_threads_user", ctypes.c_uint64),
        ("pti_threads_system", ctypes.c_uint64),
        ("pti_policy", ctypes.c_int32),
        ("pti_faults", ctypes.c_int32),
        ("pti_pageins", ctypes.c_int32),
        ("pti_cow_faults", ctypes.c_int32),
        ("pti_messages_sent", ctypes.c_int32),
        ("pti_messages_received", ctypes.c_int32),
        ("pti_syscalls_mach", ctypes.c_int32),
        ("pti_syscalls_unix", ctypes.c_int32),
        ("pti_csw", ctypes.c_int32),
        ("pti_threadnum", ctypes.c_int32),
        ("pti_numrunning", ctypes.c_int32),
        ("pti_priority", ctypes.c_int32),
    ]


class rusage_info_v4(ctypes.Structure):
    _fields_ = [
        ("ri_uuid", ctypes.c_uint8 * 16),
        ("ri_user_time", ctypes.c_uint64),
        ("ri_system_time", ctypes.c_uint64),
        ("ri_pkg_idle_wkups", ctypes.c_uint64),
        ("ri_interrupt_wkups", ctypes.c_uint64),
        ("ri_pageins", ctypes.c_uint64),
        ("ri_wired_size", ctypes.c_uint64),
        ("ri_resident_size", ctypes.c_uint64),
        ("ri_phys_footprint", ctypes.c_uint64),
        ("ri_proc_start_abstime", ctypes.c_uint64),
        ("ri_proc_exit_abstime", ctypes.c_uint64),
        ("ri_child_user_time", ctypes.c_uint64),
        ("ri_child_system_time", ctypes.c_uint64),
        ("ri_child_pkg_idle_wkups", ctypes.c_uint64),
        ("ri_child_interrupt_wkups", ctypes.c_uint64),
        ("ri_child_pageins", ctypes.c_uint64),
        ("ri_child_elapsed_abstime", ctypes.c_uint64),
        ("ri_diskio_bytesread", ctypes.c_uint64),
        ("ri_diskio_byteswritten", ctypes.c_uint64),
        ("ri_cpu_time_qos_default", ctypes.c_uint64),
        ("ri_cpu_time_qos_maintenance", ctypes.c_uint64),
        ("ri_cpu_time_qos_background", ctypes.c_uint64),
        ("ri_cpu_time_qos_utility", ctypes.c_uint64),
        ("ri_cpu_time_qos_legacy", ctypes.c_uint64),
        ("ri_cpu_time_qos_user_initiated", ctypes.c_uint64),
        ("ri_cpu_time_qos_user_interactive", ctypes.c_uint64),
        ("ri_billed_system_time", ctypes.c_uint64),
        ("ri_serviced_system_time", ctypes.c_uint64),
        ("ri_logical_writes", ctypes.c_uint64),
        ("ri_lifetime_max_phys_footprint", ctypes.c_uint64),
        ("ri_instructions", ctypes.c_uint64),
        ("ri_cycles", ctypes.c_uint64),
        ("ri_billed_energy", ctypes.c_uint64),
        ("ri_serviced_energy", ctypes.c_uint64),
        ("ri_interval_max_phys_footprint", ctypes.c_uint64),
        ("ri_runnable_time", ctypes.c_uint64),
    ]


def _libproc() -> ctypes.CDLL:
    libproc = ctypes.CDLL(LIBPROC_PATH, use_errno=True)
    libproc.proc_pidinfo.restype = ctypes.c_int
    libproc.proc_pidinfo.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int)
    libproc.proc_pid_rusage.restype = ctypes.c_int
    libproc.proc_pid_rusage.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_void_p)
    libproc.proc_listpids.restype = ctypes.c_int
    libproc.proc_listpids.argtypes = (ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int)
    return libproc


def _error() -> OSError:
    # 进程已退出时 proc_pidinfo 可能返回 0 而不设 errno。
    code = ctypes.get_errno() or errno.ESRCH
    return OSError(code, os.strerror(code))


def _pidinfo(pid: int, flavor: int, info: ctypes.Structure) -> None:
    libproc = _libproc()
    size = ctypes.sizeof(info)
    ctypes.set_errno(0)
    written = libproc.proc_pidinfo(pid, flavor, 0, ctypes.byref(info), size)
    if written <= 0:
        raise _error()
    if written != size:
        raise RuntimeError(f"proc_pidinfo(flavor={flavor}) 返回 {written} 字节，预期 {size}")


def process_memory(pid: int) -> Dict[str, int]:
    """驻留内存、物理占用（phys_footprint，macOS 下最接近 Linux 匿名内存的指标）及其历史峰值，单位字节。

    僵尸进程这里不报错：resident 是退出前的旧值，footprint 为 0。要判断进程还在不在，用 process_threads。
    """
    libproc = _libproc()
    usage = rusage_info_v4()
    ctypes.set_errno(0)
    if libproc.proc_pid_rusage(pid, RUSAGE_INFO_V4, ctypes.byref(usage)) != 0:
        raise _error()
    return {
        "resident": usage.ri_resident_size,
        "footprint": usage.ri_phys_footprint,
        "peak_footprint": usage.ri_lifetime_max_phys_footprint,
    }


def process_threads(pid: int) -> int:
    info = proc_taskinfo()
    _pidinfo(pid, PROC_PIDTASKINFO, info)
    return info.pti_threadnum


def process_created(pid: int) -> int:
    """启动时间（微秒）；pid 会被复用，与 pid 合起来才能确认还是同一个进程。"""
    info = proc_bsdinfo()
    _pidinfo(pid, PROC_PIDTBSDINFO, info)
    return info.pbi_start_tvsec * 1_000_000 + info.pbi_start_tvusec


def process_name(pid: int) -> str:
    """可执行文件名。pbi_comm 只有 15 个字符，pbi_name 更完整但可能为空。不取路径：路径里常带系统用户名。"""
    info = proc_bsdinfo()
    _pidinfo(pid, PROC_PIDTBSDINFO, info)
    return (info.pbi_name or info.pbi_comm).decode("utf-8", "replace")


def child_pids(pid: int) -> List[int]:
    libproc = _libproc()
    item = ctypes.sizeof(ctypes.c_int)
    ctypes.set_errno(0)
    needed = libproc.proc_listpids(PROC_PPID_ONLY, pid, None, 0)
    if needed <= 0:
        raise _error()
    buffer = (ctypes.c_int * (needed // item + PID_LIST_SLACK))()
    ctypes.set_errno(0)
    written = libproc.proc_listpids(PROC_PPID_ONLY, pid, buffer, ctypes.sizeof(buffer))
    # 失败时 libproc 也返回 0，与“没有子进程”只能靠 errno 区分。
    if written <= 0 and ctypes.get_errno():
        raise _error()
    return [value for value in buffer[:written // item] if value > 0]


def find_worker_pid() -> int:
    return os.getppid()


def descendants(root_pid: int) -> Dict[int, str]:
    """root_pid 的全部后代 {pid: 可执行文件名}。

    列举之后已退出、无权访问的进程，以及 proc_listpids 会列出的僵尸进程，都读不到名字，直接略过。
    """
    result: Dict[int, str] = {}
    frontier = [root_pid]
    while frontier:
        for pid in child_pids(frontier.pop()):
            try:
                result[pid] = process_name(pid)
            except OSError:
                continue
            frontier.append(pid)
    return result
