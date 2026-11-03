"""Linux procfs 与 cgroup 读取。

插件跑在插件 Runner 子进程里，拿不到 Worker 的 Python 堆，只能从进程外读 /proc 与 cgroup。
解析函数都只接收文本，便于用固定样本测试；读文件的函数对“进程在列举与读取之间退出”这类竞态
只跳过该进程，其余错误一律上抛，由采样循环最外层统一记录。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import heapq
import os
import re

PROC = Path("/proc")
THP_SYSFS = Path("/sys/kernel/mm/transparent_hugepage")

STATUS_KEYS = ("VmRSS", "VmHWM", "RssAnon", "RssFile", "RssShmem", "VmSwap", "Threads")
# glibc 非主 arena 的堆按 HEAP_MAX_SIZE 对齐映射（64 位为 64 MiB），用完的部分 rw-p、保留的尾部 ---p。
GLIBC_HEAP_MAX_SIZE = 64 * 1024 * 1024
TCP_STATE_ESTABLISHED = "01"
TCP_STATE_LISTEN = "0A"
# /proc/net/tcp 的 tr 字段：4 表示零窗口探测定时器，对端停止读取且接收窗口为 0 时出现。
TCP_TIMER_ZERO_WINDOW_PROBE = 4
RUNNER_MODULE = "src.plugin_runtime.runner.runner_main"
SMAPS_HEADER = re.compile(r"[0-9a-f]+-[0-9a-f]+ ")
# 没有名字的匿名映射按大小分档（KiB）：pymalloc 的 arena 是 1 MiB 的映射，超过 mmap 阈值的大块分配从 128 KiB 起。
ANON_SIZE_CLASSES = ((2 * 1024, "lt_2m"), (64 * 1024, "2m_to_64m"), (None, "ge_64m"))
# 最大的几个匿名映射单独列出，看是不是某一块大缓冲区（向量索引、大数组）在长。
LARGEST_ANON_MAPS = 5
# 带名字的匿名映射（[anon:名字]）按名字单列；名字由程序自己起，个数不设限的话一条记录可能很长。
NAMED_ANON_LIMIT = 10
CGROUP_V2_STAT_KEYS = ("anon", "file", "kernel", "kernel_stack", "slab", "sock", "shmem", "file_mapped")
CGROUP_V1_STAT_KEYS = ("rss", "cache", "mapped_file", "swap", "total_rss", "total_cache")


def parse_status(text: str) -> Dict[str, int]:
    """取 /proc/<pid>/status 中与内存相关的字段，内存项单位 KiB；内核没有的字段不出现在结果里。"""
    result: Dict[str, int] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep and key in STATUS_KEYS:
            result[key] = int(value.split()[0])
    return result


def parse_smaps_rollup(text: str) -> Dict[str, int]:
    """/proc/<pid>/smaps_rollup 的全部 KiB 字段（首行是区段头，跳过）。"""
    result: Dict[str, int] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        parts = value.split()
        if sep and len(parts) == 2 and parts[1] == "kB":
            result[key] = int(parts[0])
    return result


def parse_stat(text: str) -> Tuple[int, int]:
    """返回 (ppid, starttime)。comm 可能含空格和括号，所以从最后一个右括号之后切字段。"""
    fields = text[text.rindex(")") + 2 :].split()
    # 切掉 pid 与 comm 后，fields[0] 是第 3 个字段 state；ppid 是第 4 个，starttime 是第 22 个。
    return int(fields[1]), int(fields[19])


def parse_environ(raw: bytes, keys: Iterable[str]) -> Dict[str, str]:
    """只取白名单里的环境变量；Runner 与 Worker 的环境里有会话令牌，不能整份记录。"""
    wanted = set(keys)
    result: Dict[str, str] = {}
    for item in raw.split(b"\0"):
        key, sep, value = item.partition(b"=")
        name = key.decode("utf-8", "replace")
        if sep and name in wanted:
            result[name] = value.decode("utf-8", "replace")
    return result


def classify_process(exe: Optional[str], *, runner: bool) -> str:
    """按可执行文件名给 Worker 的后代进程分类，便于区分是谁在涨。

    不接收命令行：npm 等会把整条命令行（含 --api-key 之类的参数）改写进进程标题，按命令行取名会把密钥
    写进样本和聊天回复。是不是插件 Runner 由调用方判断后传入。
    """
    if runner:
        return "plugin_runner"
    if exe is None:
        return "unknown"
    name = exe.lower()
    if "chrom" in name:
        return "chromium"
    if name.startswith("node"):
        return "node"
    if name.startswith("python"):
        return "python"
    return name


def parse_exe_link(target: str) -> str:
    """/proc/<pid>/exe 链接目标的文件名；可执行文件被替换（例如升级）后内核会追加 " (deleted)"。"""
    name = os.path.basename(target)
    suffix = " (deleted)"
    return name[: -len(suffix)] if name.endswith(suffix) else name


def parse_net_tcp(text: str, port: int) -> List[Dict[str, Any]]:
    """/proc/<pid>/net/tcp 或 tcp6 中本地端口为 port 的全部条目（任意状态）。队列单位字节。

    ESTABLISHED 时 tx_queue 是未确认或未发送的字节数；LISTEN 时 rx_queue 是 accept 队列长度、tx_queue 无意义。
    """
    entries: List[Dict[str, Any]] = []
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 10:
            continue
        if int(fields[1].rsplit(":", 1)[1], 16) != port:
            continue
        tx_queue, rx_queue = (int(part, 16) for part in fields[4].split(":"))
        entries.append({
            "state": fields[3],
            "remote_port": int(fields[2].rsplit(":", 1)[1], 16),
            "tx_queue": tx_queue,
            "rx_queue": rx_queue,
            "timer": int(fields[5].split(":")[0], 16),
            "inode": int(fields[9]),
        })
    return entries


def parse_socket_link(target: str) -> Optional[int]:
    """fd 链接目标形如 socket:[12345] 时返回 inode。"""
    if target.startswith("socket:[") and target.endswith("]"):
        return int(target[len("socket:["):-1])
    return None


def glibc_heap_estimate(maps_text: str) -> Dict[str, int]:
    """按 /proc/<pid>/maps 粗估 glibc 非主 arena 的堆段。

    只统计“起点按 64 MiB 对齐的匿名 rw-p 段、紧跟一段匿名 ---p 保留尾、两者合计 64 MiB”的组合，
    即还没用满的线程堆；用满的堆没有 ---p 尾，可能与相邻映射合并，统计不到，所以这是下限估计。
    """
    regions = []
    main_heap_kb = 0
    for line in maps_text.splitlines():
        fields = line.split()
        if len(fields) < 5:
            continue
        start, end = (int(part, 16) for part in fields[0].split("-"))
        path = fields[5] if len(fields) > 5 else ""
        if path == "[heap]":
            main_heap_kb += (end - start) // 1024
        regions.append((start, end, fields[1], path))
    heaps = committed = reserved = 0
    # 相邻两段配对，第二个序列天然少一个元素。
    for (start, end, perms, path), (next_start, next_end, next_perms, next_path) in zip(regions, regions[1:], strict=False):
        if (
            not path and not next_path and perms == "rw-p" and next_perms == "---p"
            and end == next_start and start % GLIBC_HEAP_MAX_SIZE == 0
            and next_end - start == GLIBC_HEAP_MAX_SIZE
        ):
            heaps += 1
            committed += end - start
            reserved += next_end - next_start
    return {
        "heaps": heaps,
        "committed_kb": committed // 1024,
        "reserved_kb": reserved // 1024,
        "main_heap_kb": main_heap_kb,
    }


def parse_smaps(text: str) -> List[Dict[str, Any]]:
    """/proc/<pid>/smaps 的逐个映射：首行与 maps 的一行相同，其后是这个映射的各项 KiB 字段与 VmFlags。

    3.8 之前的内核没有 VmFlags，flags 为 None。
    """
    maps: List[Dict[str, Any]] = []
    for line in text.splitlines():
        if SMAPS_HEADER.match(line):
            fields = line.split(None, 5)
            start, end = (int(part, 16) for part in fields[0].split("-"))
            maps.append({
                "start": start, "end": end, "perms": fields[1], "path": fields[5] if len(fields) > 5 else "",
                "kb": {}, "flags": None,
            })
            continue
        key, sep, value = line.partition(":")
        if not sep or not maps:
            continue
        if key == "VmFlags":
            maps[-1]["flags"] = value.split()
            continue
        parts = value.split()
        if len(parts) == 2 and parts[1] == "kB":
            maps[-1]["kb"][key] = int(parts[0])
    return maps


def maps_text_of(smaps_text: str) -> str:
    """smaps 里每个映射的首行就是 maps 的一行；读过 smaps 就不必再读一遍 maps。"""
    return "\n".join(line for line in smaps_text.splitlines() if SMAPS_HEADER.match(line))


def _anon_bucket() -> Dict[str, int]:
    return {"maps": 0, "rss_kb": 0, "anon_kb": 0, "thp_kb": 0}


def _is_glibc_heap(mapping: Dict[str, Any]) -> bool:
    """glibc 用 MAP_NORESERVE 预留线程堆，VmFlags 里带 nr；紧挨着的两个线程堆会被内核合并成一段超过 64 MiB 的映射，
    这时只能靠 nr 认。overcommit_memory=2 时不设这个标志，3.8 之前的内核没有 VmFlags，所以也按 64 MiB 对齐认。"""
    flags = mapping["flags"]
    if flags is not None and "nr" in flags:
        return True
    return mapping["start"] % GLIBC_HEAP_MAX_SIZE == 0 and mapping["end"] - mapping["start"] <= GLIBC_HEAP_MAX_SIZE


def anon_breakdown(maps: List[Dict[str, Any]]) -> Dict[str, Any]:
    """按映射种类汇总常驻的匿名内存，分辨涨的是 malloc 的堆，还是堆外独立的大块映射。

    brk_heap 是主 arena（[heap]）；glibc_arenas 是线程堆（自带 jemalloc、mimalloc 的扩展也用 MAP_NORESERVE，
    会算在这里）。SQLite 页缓存、小于 mmap 阈值的分配，以及释放后没还给系统的内存都在这两类里。
    other 是其余没有名字的匿名映射，按映射大小分档：pymalloc 的 arena、超过 mmap 阈值的大块分配（faiss 索引、
    numpy 数组等）在这里；内核会合并相邻的同权限匿名映射，大小分档只是粗分。带名字的匿名映射（[stack]、[anon:名字]）
    按名字单列。不记地址与文件路径。
    """
    other = {name: _anon_bucket() for _, name in ANON_SIZE_CLASSES}
    result: Dict[str, Any] = {"brk_heap": _anon_bucket(), "glibc_arenas": _anon_bucket(), "other": other}
    named: Dict[str, Dict[str, int]] = {}
    ranked: List[Tuple[int, str, int, Dict[str, int]]] = []
    for mapping in maps:
        path = mapping["path"]
        anonymous = path in ("", "[heap]") or path.startswith(("[stack", "[anon:"))
        # 不可访问的映射（线程堆的保留尾、栈保护页）不占内存。
        if not anonymous or mapping["perms"].startswith("---"):
            continue
        size_kb = (mapping["end"] - mapping["start"]) // 1024
        if path == "[heap]":
            kind, bucket = "brk_heap", result["brk_heap"]
        elif path:
            # 4.5 之前的内核把线程栈记作 [stack:线程号]，并成一项，免得每个线程各占一个名字。
            kind = "[stack]" if path.startswith("[stack") else path
            bucket = named.setdefault(kind, _anon_bucket())
        elif mapping["perms"] == "rw-p" and _is_glibc_heap(mapping):
            kind, bucket = "glibc_arena", result["glibc_arenas"]
        else:
            kind = next(name for limit, name in ANON_SIZE_CLASSES if limit is None or size_kb < limit)
            bucket = other[kind]
        kb = mapping["kb"]
        # 内核没编进透明大页时没有 AnonHugePages 这一项，也就没有大页。
        values = {"rss_kb": kb["Rss"], "anon_kb": kb["Anonymous"], "thp_kb": kb.get("AnonHugePages", 0)}
        bucket["maps"] += 1
        for key, value in values.items():
            bucket[key] += value
        ranked.append((values["rss_kb"], kind, size_kb, values))
    kept = sorted(named.items(), key=lambda item: -item[1]["rss_kb"])
    result["named"] = dict(kept[:NAMED_ANON_LIMIT])
    if len(kept) > NAMED_ANON_LIMIT:
        rest = _anon_bucket()
        for _, bucket in kept[NAMED_ANON_LIMIT:]:
            for key in rest:
                rest[key] += bucket[key]
        result["named"]["_rest"] = rest
    result["largest"] = [
        {"kind": kind, "size_kb": size_kb, **values}
        for _, kind, size_kb, values in heapq.nlargest(LARGEST_ANON_MAPS, ranked, key=lambda item: item[0])
    ]
    return result


def parse_thp_mode(text: str) -> str:
    """sysfs 里透明大页的开关形如 "always [madvise] never"，方括号里是当前选中的那项。"""
    match = re.search(r"\[([^\]]+)\]", text)
    if match is None:
        raise ValueError(f"无法识别的透明大页设置: {text!r}")
    return match.group(1)


def _unescape_mount_field(value: str) -> str:
    # mountinfo 把空格、制表符、换行与反斜杠写成三位八进制转义。
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


def parse_mountinfo_cgroups(text: str) -> Tuple[List[Tuple[str, str]], List[Tuple[str, str]]]:
    """从 /proc/<pid>/mountinfo 取 (cgroup2 挂载, 带 memory 控制器的 v1 挂载)，按出现顺序，每项为 (挂载 root, 挂载点)。"""
    v2: List[Tuple[str, str]] = []
    v1: List[Tuple[str, str]] = []
    for line in text.splitlines():
        left, sep, right = line.partition(" - ")
        fields, tail = left.split(), right.split()
        if not sep or len(fields) < 5 or not tail:
            continue
        mount = (_unescape_mount_field(fields[3]), _unescape_mount_field(fields[4]))
        if tail[0] == "cgroup2":
            v2.append(mount)
        elif tail[0] == "cgroup" and len(tail) >= 3 and "memory" in tail[2].split(","):
            v1.append(mount)
    return v2, v1


def cgroup_dir(mount: Tuple[str, str], cgroup_path: str) -> Optional[Path]:
    """把 /proc/<pid>/cgroup 里的路径换算成挂载点下的目录。

    容器里挂载的往往不是 cgroup 树的根：cgroup v1 下 Docker 默认共享宿主的 cgroup 命名空间，
    /proc/<pid>/cgroup 显示 /docker/<id>，而容器内的挂载点本身就是这一层（挂载 root 为 /docker/<id>）。
    路径不在挂载 root 之下时返回 None，表示该 cgroup 在本命名空间里看不到。
    """
    root, mountpoint = mount
    if root == "/":
        return Path(mountpoint) / cgroup_path.lstrip("/")
    if cgroup_path == root or cgroup_path.startswith(root.rstrip("/") + "/"):
        return Path(mountpoint) / cgroup_path[len(root):].lstrip("/")
    return None


def visible_cgroup_dir(mounts: List[Tuple[str, str]], cgroup_path: str) -> Optional[Path]:
    """同一挂载点上后挂的会遮住先挂的（例如在私有 cgroup 命名空间里原地再挂一层），从后往前取第一个能换算出目录的。"""
    for mount in reversed(mounts):
        directory = cgroup_dir(mount, cgroup_path)
        if directory is not None:
            return directory
    return None


def parse_cgroup_file(text: str) -> Tuple[Optional[str], Optional[str]]:
    """从 /proc/<pid>/cgroup 取 (v2 路径, v1 memory 控制器路径)，不存在的为 None。"""
    v2: Optional[str] = None
    v1: Optional[str] = None
    for line in text.splitlines():
        hierarchy, _, rest = line.partition(":")
        controllers, _, path = rest.partition(":")
        if hierarchy == "0" and controllers == "":
            v2 = path
        elif "memory" in controllers.split(","):
            v1 = path
    return v2, v1


def parse_flat_keyed(text: str, keys: Iterable[str]) -> Dict[str, int]:
    """memory.stat / memory.events 这类“键 值”逐行文件。"""
    wanted = set(keys)
    result: Dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] in wanted:
            result[parts[0]] = int(parts[1])
    return result


def parse_limit(text: str) -> Optional[int]:
    """cgroup v2 的 memory.max 未设上限时是 "max"，返回 None 表示不限。"""
    value = text.strip()
    return None if value == "max" else int(value)


# ---------------------------------------------------------------------------
# 读文件（只在 Linux 上调用）
# ---------------------------------------------------------------------------


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def thp_settings() -> Optional[Dict[str, str]]:
    """系统的透明大页设置；内核没编进 THP 时没有这个目录，返回 None。容器里看到的是宿主机内核的设置。"""
    if not THP_SYSFS.is_dir():
        return None
    return {name: parse_thp_mode(read_text(THP_SYSFS / name)) for name in ("enabled", "defrag")}


def process_snapshot(pid: int) -> Dict[str, int]:
    """status 的内存字段；僵尸进程与正在退出的进程已释放地址空间，结果里没有 VmRSS 等内存项。"""
    return parse_status(read_text(PROC / str(pid) / "status"))


def anon_bytes(pid: int, status: Dict[str, int]) -> int:
    """匿名常驻内存（字节）。

    4.5 之前的内核 status 没有 RssAnon，改用 statm 的 resident - shared：两者来自同一组页计数器，
    shared 是文件页加共享内存页，差值就是匿名页。
    """
    if "RssAnon" in status:
        return status["RssAnon"] * 1024
    resident, shared = (int(part) for part in read_text(PROC / str(pid) / "statm").split()[1:3])
    return (resident - shared) * os.sysconf("SC_PAGE_SIZE")


def exe_name(pid: int) -> Optional[str]:
    try:
        return parse_exe_link(os.readlink(PROC / str(pid) / "exe"))
    except (FileNotFoundError, PermissionError):
        # 僵尸进程没有 exe 链接；其他用户的进程无权读取。两者都只影响分类名。
        return None


def process_identity(pid: int) -> Tuple[int, int]:
    """(ppid, starttime)；starttime 与 pid 一起唯一标识一个进程实例，用来发现 Worker 已被重启。"""
    return parse_stat(read_text(PROC / str(pid) / "stat"))


def find_worker_pid() -> int:
    """Runner 是 Worker 的直接子进程（Linux venv 的 python 是符号链接，没有中间启动器）。

    不用命令行认 Worker：1.2.3 的守护进程与 Worker 命令行相同；也不用 MAIBOT_WORKER_PROCESS：Runner 同样继承了它。
    只做一个防御：父进程若也是插件 Runner，说明进程树不是预期形状，直接报错。
    """
    parent = os.getppid()
    if RUNNER_MODULE in " ".join(
        part.decode("utf-8", "replace") for part in (PROC / str(parent) / "cmdline").read_bytes().split(b"\0")
    ):
        raise RuntimeError(f"父进程 {parent} 也是插件 Runner，进程树形状不符，无法确定 Worker")
    return parent


def descendants(root_pid: int) -> Tuple[Dict[int, str], int]:
    """root_pid 的全部后代进程 {pid: 分类}，以及因权限读不到的进程数。

    扫描 /proc 而不是读 children 文件：后者依赖内核编译选项。/proc 以 hidepid=1 挂载时其他用户的进程
    仍会列出、但读取报 EPERM；它们不可能是 Worker 的同用户后代之外还需要统计的进程，只计数。
    """
    parent_of: Dict[int, int] = {}
    hidden = 0
    for entry in PROC.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            parent_of[int(entry.name)] = parse_stat(read_text(entry / "stat"))[0]
        except (FileNotFoundError, ProcessLookupError):
            # 列目录与读文件之间进程已退出，属于正常竞态。
            continue
        except PermissionError:
            hidden += 1
    children: Dict[int, List[int]] = {}
    for pid, ppid in parent_of.items():
        children.setdefault(ppid, []).append(pid)
    result: Dict[int, str] = {}
    frontier = list(children.get(root_pid, []))
    while frontier:
        pid = frontier.pop()
        runner = False
        if parent_of[pid] == root_pid:
            # 宿主只把插件 Runner 拉成 Worker 的直接子进程；Runner 里 fork 出的子进程不 exec，命令行与 Runner 相同，
            # 所以只在直接子进程上认 Runner。
            try:
                runner = RUNNER_MODULE.encode() in (PROC / str(pid) / "cmdline").read_bytes()
            except (FileNotFoundError, ProcessLookupError):
                continue
        result[pid] = classify_process(exe_name(pid), runner=runner)
        frontier.extend(children.get(pid, []))
    return result, hidden


def fd_summary(pid: int, watched: Dict[str, Path]) -> Dict[str, int]:
    """按类型统计 fd；watched 里的文件（主库、元数据库）单独计数，用来估计 SQLite 连接数。"""
    counts: Dict[str, int] = {"total": 0, "socket": 0, "pipe": 0, "anon_inode": 0, "file": 0}
    targets = {str(path): name for name, path in watched.items()}
    for name in targets.values():
        counts[f"open_{name}"] = 0
    fd_dir = PROC / str(pid) / "fd"
    for entry in os.listdir(fd_dir):
        try:
            target = os.readlink(fd_dir / entry)
        except FileNotFoundError:
            # 列目录之后 fd 已关闭。
            continue
        counts["total"] += 1
        if target.startswith("socket:"):
            counts["socket"] += 1
        elif target.startswith("pipe:"):
            counts["pipe"] += 1
        elif target.startswith("anon_inode:"):
            counts["anon_inode"] += 1
        else:
            counts["file"] += 1
            watched_name = targets.get(target)
            if watched_name is not None:
                counts[f"open_{watched_name}"] += 1
    return counts


def socket_inodes(pid: int) -> Set[int]:
    inodes: Set[int] = set()
    fd_dir = PROC / str(pid) / "fd"
    for entry in os.listdir(fd_dir):
        try:
            inode = parse_socket_link(os.readlink(fd_dir / entry))
        except FileNotFoundError:
            continue
        if inode is not None:
            inodes.add(inode)
    return inodes


def webui_connections(pid: int, port: int) -> Dict[str, Any]:
    """WebUI 端口上属于 Worker 的监听与已建立连接。

    net/tcp 是整个网络命名空间的表，同端口的条目不一定属于 Worker（例如同机另一个实例），
    所以用 Worker 持有的 socket inode 认归属。WebUI 启动时端口冲突只记日志，enabled 为真也可能没在监听。
    """
    entries: List[Dict[str, Any]] = []
    for name in ("tcp", "tcp6"):
        path = PROC / str(pid) / "net" / name
        if path.exists():
            entries.extend(parse_net_tcp(read_text(path), port))
    owned = socket_inodes(pid)
    mine = [entry for entry in entries if entry["inode"] in owned]
    return {
        "listening": any(entry["state"] == TCP_STATE_LISTEN for entry in mine),
        "conns": [entry for entry in mine if entry["state"] == TCP_STATE_ESTABLISHED],
    }


def cgroup_memory(pid: int) -> Dict[str, object]:
    """进程所在 cgroup 的内存用量、上限与 OOM 计数。

    按 mountinfo 找挂载点并换算目录，不假定挂载在 /sys/fs/cgroup、也不假定挂载的是树根。
    memory 控制器只能挂在一个层级上：混合模式下它在 v1，统一层级里没有它，所以先看 v1。
    目录不可见、memory 控制器未启用（如树莓派默认内核）或位于根 cgroup 时，只记 unavailable 与原因：
    这是这台机器的事实，不是采样失败。
    """
    v2_path, v1_path = parse_cgroup_file(read_text(PROC / str(pid) / "cgroup"))
    v2_mounts, v1_mounts = parse_mountinfo_cgroups(read_text(PROC / str(pid) / "mountinfo"))
    if v1_path is not None and v1_mounts:
        base = visible_cgroup_dir(v1_mounts, v1_path)
        if base is None or not (base / "memory.limit_in_bytes").exists():
            return {"version": 1, "path": v1_path, "unavailable": "cgroup 目录在本挂载命名空间里不可见"}
        limit = int(read_text(base / "memory.limit_in_bytes"))
        return {
            "version": 1,
            "path": v1_path,
            "current": int(read_text(base / "memory.usage_in_bytes")),
            "peak": int(read_text(base / "memory.max_usage_in_bytes")),
            # v1 未设上限时是一个接近 2^63 的页对齐值。
            "max": None if limit >= 1 << 62 else limit,
            "stat": parse_flat_keyed(read_text(base / "memory.stat"), CGROUP_V1_STAT_KEYS),
            "events": parse_flat_keyed(read_text(base / "memory.oom_control"), ("oom_kill", "under_oom")),
        }
    if v2_path is not None and v2_mounts:
        base = visible_cgroup_dir(v2_mounts, v2_path)
        if base is None:
            return {"version": 2, "path": v2_path, "unavailable": "cgroup 目录在本挂载命名空间里不可见"}
        if not (base / "memory.current").exists():
            # memory.current/max/events 不出现在根 cgroup 上，也不出现在未启用 memory 控制器的 cgroup 上。
            return {"version": 2, "path": v2_path, "unavailable": "未启用 memory 控制器或位于根 cgroup"}
        result: Dict[str, object] = {"version": 2, "path": v2_path}
        result["current"] = int(read_text(base / "memory.current"))
        result["max"] = parse_limit(read_text(base / "memory.max"))
        peak = base / "memory.peak"
        if peak.exists():
            result["peak"] = int(read_text(peak))
        result["stat"] = parse_flat_keyed(read_text(base / "memory.stat"), CGROUP_V2_STAT_KEYS)
        result["events"] = parse_flat_keyed(read_text(base / "memory.events"), ("oom", "oom_kill", "max", "high"))
        return result
    return {"version": None}
