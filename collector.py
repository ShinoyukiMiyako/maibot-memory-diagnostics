"""采样编排：快速样本、趋势窗口、关键文件。只做同步读取，由插件放进线程里调用。

快速样本（默认 15 秒）只读 status 与 TCP 表，开销小，用来抓统计任务的 15 分钟锯齿和回放阶跃；
趋势记录（默认 5 分钟）汇总窗口内的最小/最大/最新值，并附上 smaps_rollup、堆段估计、fd、cgroup 与文件大小，
长期保留，用来看 30-60 天的走势。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import os
import platform
import sqlite3
import sys
import time

from . import procfs

IS_LINUX = sys.platform.startswith("linux")
IS_WINDOWS = sys.platform == "win32"
IS_DARWIN = sys.platform == "darwin"
# 宿主卸载或重载插件时会把插件目录下的模块从 sys.modules 清掉，函数内的相对导入届时会失败，所以只在模块顶层导入。
if IS_WINDOWS:
    from . import winproc
if IS_DARWIN:
    from . import darwinproc
# 主指标在各平台是不同的量，摘要要如实写名字。
PRIMARY_LABEL = "匿名内存" if IS_LINUX else "提交内存" if IS_WINDOWS else "物理占用"

# 只从 Worker 的环境里取与分配器有关的键；Runner 与 Worker 的环境里有会话令牌，不能整份记录。
WORKER_ENV_WHITELIST = ("MALLOC_ARENA_MAX", "GLIBC_TUNABLES", "MALLOC_TRIM_THRESHOLD_", "MALLOC_MMAP_THRESHOLD_",
                        "MALLOC_TOP_PAD_", "MALLOC_MMAP_MAX_", "PYTHONMALLOC")
# WebUI 连接发送队列超过这个值且处于零窗口探测，基本可以认定客户端已停止读取。
STALLED_TX_QUEUE_BYTES = 256 * 1024


class WorkerGone(RuntimeError):
    """Worker 进程已退出或被替换（pid 复用），继续采样没有意义。"""


@dataclass
class Targets:
    worker_pid: int
    worker_identity: Tuple[Any, ...]
    instance_root: Path
    webui_port: Optional[int]
    watched_files: Dict[str, Path]
    db_files: Dict[str, Path]
    watched_dirs: Dict[str, Tuple[Path, str]] = field(default_factory=dict)


@dataclass
class Window:
    """一个趋势窗口内 Worker 主指标的极值，主指标在 Linux 是 RssAnon，Windows 是 PrivateUsage，macOS 是 phys_footprint。"""

    count: int = 0
    primary_min: Optional[int] = None
    primary_max: Optional[int] = None
    primary_last: Optional[int] = None
    rss_max: Optional[int] = None
    hwm_last: Optional[int] = None
    rpc_ms_max: Optional[float] = None
    # 没采 WebUI（Windows、未启用 WebUI）时保持 None，与“测得 0”区分开。
    webui_tx_max: Optional[int] = None
    webui_stalled_max: Optional[int] = None

    def add(self, sample: Dict[str, Any]) -> None:
        worker = sample["worker"]
        primary = worker["primary"]
        self.count += 1
        self.primary_min = primary if self.primary_min is None else min(self.primary_min, primary)
        self.primary_max = primary if self.primary_max is None else max(self.primary_max, primary)
        self.primary_last = primary
        self.rss_max = worker["rss"] if self.rss_max is None else max(self.rss_max, worker["rss"])
        self.hwm_last = worker.get("hwm")
        rpc_ms = sample.get("rpc_ms")
        if rpc_ms is not None:
            self.rpc_ms_max = rpc_ms if self.rpc_ms_max is None else max(self.rpc_ms_max, rpc_ms)
        webui = sample.get("webui")
        if webui is not None:
            self.webui_tx_max = webui["tx_max"] if self.webui_tx_max is None else max(self.webui_tx_max, webui["tx_max"])
            self.webui_stalled_max = (
                webui["stalled"] if self.webui_stalled_max is None else max(self.webui_stalled_max, webui["stalled"])
            )

    def as_record(self) -> Dict[str, Any]:
        return {
            "samples": self.count,
            "primary_min": self.primary_min,
            "primary_max": self.primary_max,
            "primary_last": self.primary_last,
            "rss_max": self.rss_max,
            "hwm_last": self.hwm_last,
            "rpc_ms_max": self.rpc_ms_max,
            "webui_tx_max": self.webui_tx_max,
            "webui_stalled_max": self.webui_stalled_max,
        }


def ensure_supported_platform() -> None:
    if not (IS_LINUX or IS_WINDOWS or IS_DARWIN):
        raise NotImplementedError(f"内存诊断插件只支持 Linux、Windows 与 macOS，当前平台 {sys.platform}")


def identity_of(pid: int) -> Tuple[Any, ...]:
    """(pid, 启动时间)：Linux 是 stat 的 starttime，Windows 与 macOS 是创建时间。会原样写进每条记录，不含路径。"""
    ensure_supported_platform()
    if IS_LINUX:
        return (pid, procfs.process_identity(pid)[1])
    if IS_WINDOWS:
        return (pid, winproc.process_created(pid))
    return (pid, darwinproc.process_created(pid))


def find_worker() -> Tuple[int, Tuple[Any, ...]]:
    ensure_supported_platform()
    if IS_LINUX:
        pid = procfs.find_worker_pid()
    elif IS_WINDOWS:
        pid = winproc.find_worker_pid()
    else:
        pid = darwinproc.find_worker_pid()
    return pid, identity_of(pid)


@dataclass
class Collector:
    targets: Targets
    window: Window = field(default_factory=Window)

    def check_worker(self) -> None:
        pid = self.targets.worker_pid
        # Worker 被杀后 Runner 不会自行退出（宿主断连不结束 Runner），Linux 与 macOS 上会被收养，父进程号随之改变。
        if not IS_WINDOWS and os.getppid() != pid:
            raise WorkerGone(f"父进程已从 Worker pid={pid} 变为 {os.getppid()}，Worker 已退出")
        try:
            current = identity_of(pid)
        except OSError as exc:
            raise WorkerGone(f"Worker pid={pid} 已不可访问: {exc}") from exc
        if current != self.targets.worker_identity:
            raise WorkerGone(f"Worker pid={pid} 已被替换: {self.targets.worker_identity} -> {current}")

    # -- 快速样本 ---------------------------------------------------------

    def fast(self) -> Dict[str, Any]:
        """先确认 Worker 还在，再读进程外的内存。不碰 Worker 主循环：主循环卡住时这一步照样能采。"""
        self.check_worker()
        sample: Dict[str, Any] = {"t": round(time.time(), 3), "k": "fast"}
        if IS_LINUX:
            sample.update(self._fast_linux())
        elif IS_WINDOWS:
            sample.update(self._fast_windows())
        else:
            sample.update(self._fast_darwin())
        return sample

    def commit_fast(self, sample: Dict[str, Any]) -> None:
        """把附上 RPC 探测结果的快速样本计入当前趋势窗口。"""
        self.window.add(sample)

    def _fast_linux(self) -> Dict[str, Any]:
        pid = self.targets.worker_pid
        status = procfs.process_snapshot(pid)
        if "VmRSS" not in status:
            # 地址空间已释放：Worker 已退出、等待父进程回收。
            raise WorkerGone(f"Worker pid={pid} 正在退出（status 已没有内存字段）")
        worker = {
            "rss": status["VmRSS"] * 1024,
            "primary": procfs.anon_bytes(pid, status),
            "hwm": status["VmHWM"] * 1024,
            "swap": status["VmSwap"] * 1024,
            "threads": status["Threads"],
        }
        children: Dict[str, Dict[str, int]] = {}
        kinds, hidden = procfs.descendants(pid)
        for child_pid, kind in kinds.items():
            bucket = children.setdefault(kind, {"n": 0, "rss": 0, "anon": 0})
            try:
                child = procfs.process_snapshot(child_pid)
                if "VmRSS" not in child:
                    # 僵尸进程或正在退出的进程：没有内存可计，单独计数，不当作 0 内存的活进程。
                    bucket["exiting"] = bucket.get("exiting", 0) + 1
                    continue
                child_anon = procfs.anon_bytes(child_pid, child)
            except (FileNotFoundError, ProcessLookupError):
                # 子进程在列举之后退出（例如浏览器渲染进程），不计入本次样本。
                continue
            bucket["n"] += 1
            bucket["rss"] += child["VmRSS"] * 1024
            bucket["anon"] += child_anon
        own_pid = os.getpid()
        own = procfs.process_snapshot(own_pid)
        result: Dict[str, Any] = {
            "worker": worker,
            "children": {kind: bucket for kind, bucket in children.items() if bucket["n"] or bucket.get("exiting")},
            "self": {"rss": own["VmRSS"] * 1024, "anon": procfs.anon_bytes(own_pid, own)},
        }
        if hidden:
            result["proc_hidden"] = hidden
        if self.targets.webui_port is not None:
            result["webui"] = summarize_connections(procfs.webui_connections(pid, self.targets.webui_port))
        return result

    def _fast_windows(self) -> Dict[str, Any]:
        pid = self.targets.worker_pid
        memory = winproc.process_memory(pid)
        worker = {
            "rss": memory["working_set"],
            "primary": memory["private_usage"],
            "hwm": memory["peak_working_set"],
            "handles": memory["handle_count"],
        }
        children: Dict[str, Dict[str, int]] = {}
        for child_pid, exe in winproc.descendants(pid).items():
            try:
                child = winproc.process_memory(child_pid)
            except OSError:
                # 子进程在列举之后退出，或是无权打开的进程（例如以管理员身份运行的浏览器）。
                continue
            bucket = children.setdefault(exe.lower(), {"n": 0, "rss": 0, "anon": 0})
            bucket["n"] += 1
            bucket["rss"] += child["working_set"]
            bucket["anon"] += child["private_usage"]
        own = winproc.process_memory(os.getpid())
        # Windows 取不到 TCP 发送队列长度，不记 webui。
        return {"worker": worker, "children": children, "self": {"rss": own["working_set"], "anon": own["private_usage"]}}

    def _fast_darwin(self) -> Dict[str, Any]:
        pid = self.targets.worker_pid
        try:
            memory = darwinproc.process_memory(pid)
            # 放在内存之后：僵尸进程的内存读数不报错、数值是残留的，线程数才会报 ESRCH，这条样本不能写进去。
            threads = darwinproc.process_threads(pid)
        except ProcessLookupError as exc:
            raise WorkerGone(f"Worker pid={pid} 正在退出: {exc}") from exc
        worker = {
            "rss": memory["resident"],
            "primary": memory["footprint"],
            "hwm": memory["peak_footprint"],
            "threads": threads,
        }
        children: Dict[str, Dict[str, int]] = {}
        for child_pid, exe in darwinproc.descendants(pid).items():
            try:
                child = darwinproc.process_memory(child_pid)
            except OSError:
                # 子进程在列举之后退出，或是无权读取的进程。
                continue
            bucket = children.setdefault(exe.lower(), {"n": 0, "rss": 0, "anon": 0})
            bucket["n"] += 1
            bucket["rss"] += child["resident"]
            bucket["anon"] += child["footprint"]
        own = darwinproc.process_memory(os.getpid())
        # 不记 webui：要逐个 socket 读另一个进程的发送队列，先做与 Windows 相同的子集。
        return {"worker": worker, "children": children, "self": {"rss": own["resident"], "anon": own["footprint"]}}

    # -- 趋势记录 ---------------------------------------------------------

    def trend(self) -> Dict[str, Any]:
        """窗口不在这里清空：调用方把记录写盘之后再调 reset_window，这一轮失败时窗口里的样本并进下一条记录。"""
        self.check_worker()
        record: Dict[str, Any] = {
            "t": round(time.time(), 3),
            "k": "trend",
            "wid": list(self.targets.worker_identity),
            "window": self.window.as_record(),
        }
        if IS_LINUX:
            pid = self.targets.worker_pid
            proc = procfs.PROC / str(pid)
            # smaps_rollup 与 maps 读取时持有 Worker 的 mmap 读锁，只在趋势节拍读；4.14 之前的内核没有 smaps_rollup。
            rollup = proc / "smaps_rollup"
            record["smaps"] = procfs.parse_smaps_rollup(procfs.read_text(rollup)) if rollup.exists() else None
            record["heap"] = procfs.glibc_heap_estimate(procfs.read_text(proc / "maps"))
            record["fds"] = procfs.fd_summary(pid, self.targets.db_files)
            record["cgroup"] = procfs.cgroup_memory(pid)
            if self.targets.webui_port is not None:
                record["webui_conns"] = procfs.webui_connections(pid, self.targets.webui_port)["conns"]
        record["files"] = file_stats({**self.targets.watched_files, **self.targets.db_files, **sidecar_files(self.targets.db_files)})
        record["dirs"] = {name: dir_stats(path, pattern) for name, (path, pattern) in self.targets.watched_dirs.items()}
        return record

    def reset_window(self) -> None:
        self.window = Window()

    # -- 元信息 -----------------------------------------------------------

    def meta(self, extra: Dict[str, Any]) -> Dict[str, Any]:
        record: Dict[str, Any] = {
            "t": round(time.time(), 3),
            "k": "meta",
            "platform": sys.platform,
            "python": sys.version.split()[0],
            # SQLite 3.43 之前没有 octet_length，黑话证据体量按字符而不是字节计。
            "sqlite": sqlite3.sqlite_version,
            "cpu_count": os.cpu_count(),
            # 宿主只给 Runner 设置这个变量，Worker 的环境里没有。
            "host_version": os.environ.get("MAIBOT_HOST_VERSION"),
            "worker_pid": self.targets.worker_pid,
            "wid": list(self.targets.worker_identity),
            "webui_port": self.targets.webui_port,
        }
        if IS_LINUX:
            record["kernel"] = os.uname().release
            record["libc"] = os.confstr("CS_GNU_LIBC_VERSION") if "CS_GNU_LIBC_VERSION" in os.confstr_names else None
            record["worker_env"] = procfs.parse_environ(
                (procfs.PROC / str(self.targets.worker_pid) / "environ").read_bytes(), WORKER_ENV_WHITELIST
            )
            record["cgroup"] = procfs.cgroup_memory(self.targets.worker_pid)
        if IS_DARWIN:
            record["kernel"] = os.uname().release
            record["macos"] = platform.mac_ver()[0]
        record.update(extra)
        return record


def summarize_connections(webui: Dict[str, Any]) -> Dict[str, Any]:
    connections: List[Dict[str, Any]] = webui["conns"]
    stalled = [
        conn for conn in connections
        if conn["timer"] == procfs.TCP_TIMER_ZERO_WINDOW_PROBE and conn["tx_queue"] >= STALLED_TX_QUEUE_BYTES
    ]
    return {
        "listening": webui["listening"],
        "conns": len(connections),
        "stalled": len(stalled),
        "tx_max": max((conn["tx_queue"] for conn in connections), default=0),
        "tx_sum": sum(conn["tx_queue"] for conn in connections),
    }


def sidecar_files(db_files: Dict[str, Path]) -> Dict[str, Path]:
    """SQLite WAL 模式的 -wal 与 -shm 文件。"""
    result: Dict[str, Path] = {}
    for name, path in db_files.items():
        result[f"{name}_wal"] = path.with_name(path.name + "-wal")
        result[f"{name}_shm"] = path.with_name(path.name + "-shm")
    return result


def file_stats(files: Dict[str, Path]) -> Dict[str, Optional[Dict[str, float]]]:
    """文件大小与修改时间；不存在的文件记 None（例如 1.2.3 没有 memory.jsonl、未开 WAL 时没有 -wal）。"""
    result: Dict[str, Optional[Dict[str, float]]] = {}
    for name, path in files.items():
        try:
            stat = path.stat()
        except FileNotFoundError:
            result[name] = None
            continue
        result[name] = {"size": stat.st_size, "mtime": round(stat.st_mtime, 3)}
    return result


def dir_stats(directory: Path, pattern: str) -> Optional[Dict[str, int]]:
    """目录下匹配 pattern 的文件个数与总字节数；目录不存在记 None。"""
    if not directory.is_dir():
        return None
    count = total = 0
    for path in directory.glob(pattern):
        try:
            total += path.stat().st_size
        except FileNotFoundError:
            # 日志轮转时文件在列举后被删除。
            continue
        count += 1
    return {"files": count, "bytes": total}
