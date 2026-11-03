import os

import pytest

from memdiag import procfs

STATUS = """Name:\tpython
Umask:\t0022
State:\tS (sleeping)
Pid:\t4242
PPid:\t4200
VmPeak:\t 3123456 kB
VmHWM:\t 2101234 kB
VmRSS:\t 1987654 kB
RssAnon:\t 1800000 kB
RssFile:\t  187000 kB
RssShmem:\t     654 kB
VmSwap:\t       0 kB
Threads:\t37
"""

SMAPS_ROLLUP = """55d0c0a00000-7ffd1e5ff000 ---p 00000000 00:00 0                          [rollup]
Rss:             1987654 kB
Pss:             1980000 kB
Pss_Anon:        1799000 kB
Pss_File:         180000 kB
Anonymous:       1800000 kB
AnonHugePages:         0 kB
Swap:                  0 kB
"""

NET_TCP = """  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 0100007F:4A6A 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 41234 1 0000000000000000 100 0 0 10 0
   1: 0100007F:4A6A 0100007F:D2F0 01 003D6C20:00000000 04:00000F2A 00000000  1000        0 51234 2 0000000000000000 20 4 30 10 -1
   2: 0100007F:4A6A 0100007F:D2F4 01 00000000:00000000 02:00000AF0 00000000  1000        0 51240 1 0000000000000000 20 4 30 10 -1
   3: 0100007F:1F90 0100007F:D300 01 00000100:00000000 00:00000000 00000000  1000        0 51250 1 0000000000000000 20 4 30 10 -1
"""

NET_TCP6 = """  sl  local_address                         remote_address                        st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 00000000000000000000000001000000:4A6A 00000000000000000000000001000000:D400 01 00001000:00000000 04:00000100 00000000  1000        0 61234 1 0000000000000000 20 4 30 10 -1
"""

MAPS = """55d0c0a00000-55d0c0c00000 rw-p 00000000 00:00 0                          [heap]
7f2c00000000-7f2c02a21000 rw-p 00000000 00:00 0
7f2c02a21000-7f2c04000000 ---p 00000000 00:00 0
7f2c08000000-7f2c08100000 rw-p 00000000 00:00 0
7f2c08100000-7f2c0c000000 ---p 00000000 00:00 0
7f2c10000000-7f2c10021000 rw-p 00000000 00:00 0
7f2c10021000-7f2c10200000 ---p 00000000 00:00 0
7f2c14001000-7f2c15001000 rw-p 00000000 00:00 0
7f2c15001000-7f2c18001000 ---p 00000000 00:00 0
7f2c20000000-7f2c20100000 r-xp 00000000 08:01 1234                       /usr/lib/libfoo.so
"""

SMAPS = """55d0c0a00000-55d0c0c00000 rw-p 00000000 00:00 0                          [heap]
Size:               2048 kB
Rss:                1500 kB
Anonymous:          1500 kB
AnonHugePages:         0 kB
THPeligible:    0
VmFlags: rd wr mr mw me ac
7f2c00000000-7f2c02a21000 rw-p 00000000 00:00 0
Rss:               40000 kB
Anonymous:         40000 kB
AnonHugePages:      2048 kB
VmFlags: rd wr mr mw me nr sd
7f2c02a21000-7f2c04000000 ---p 00000000 00:00 0
Rss:                   0 kB
Anonymous:             0 kB
VmFlags: mr mw me nr sd
7f2c08000000-7f2c0c000000 rw-p 00000000 00:00 0
Rss:               60000 kB
Anonymous:         60000 kB
AnonHugePages:      4096 kB
VmFlags: rd wr mr mw me nr sd
7f2c14001000-7f2c15001000 rw-p 00000000 00:00 0
Rss:               16000 kB
Anonymous:         16000 kB
VmFlags: rd wr mr mw me ac sd
7f2c20000000-7f2c30000000 rw-p 00000000 00:00 0
Rss:              200000 kB
Anonymous:        200000 kB
AnonHugePages:      8192 kB
VmFlags: rd wr mr mw me ac sd
7f2c40000000-7f2c40100000 rw-p 00000000 00:00 0                          [anon:cpython:pymalloc]
Rss:                1024 kB
Anonymous:          1024 kB
7f2c41000000-7f2c41080000 rw-p 00000000 00:00 0
Rss:                 300 kB
Anonymous:           300 kB
7f2c48000000-7f2c4e000000 rw-p 00000000 00:00 0
Rss:               90000 kB
Anonymous:         90000 kB
VmFlags: rd wr mr mw me nr sd
7f2c50000000-7f2c50100000 r-xp 00000000 08:01 1234                       /usr/lib/lib foo.so
Rss:                 500 kB
Anonymous:             0 kB
7ffd1e5de000-7ffd1e5ff000 rw-p 00000000 00:00 0                          [stack]
Rss:                 100 kB
Anonymous:           100 kB
ffffffffff600000-ffffffffff601000 --xp 00000000 00:00 0                  [vsyscall]
Rss:                   0 kB
"""

RUNNER_CMDLINE = b"/opt/venv/bin/python\0-m\0src.plugin_runtime.runner.runner_main\0"


def test_parse_status_reads_memory_fields_in_kib():
    result = procfs.parse_status(STATUS)
    assert result == {
        "VmHWM": 2101234, "VmRSS": 1987654, "RssAnon": 1800000, "RssFile": 187000,
        "RssShmem": 654, "VmSwap": 0, "Threads": 37,
    }


def test_parse_smaps_rollup_skips_header_line():
    result = procfs.parse_smaps_rollup(SMAPS_ROLLUP)
    assert result["Pss_Anon"] == 1799000
    assert result["Anonymous"] == 1800000
    assert "55d0c0a00000-7ffd1e5ff000 ---p 00000000 00" not in result


def test_parse_stat_handles_comm_with_spaces_and_parens():
    fields_after_comm = " ".join(["S", "4200"] + ["0"] * 17 + ["987654"] + ["0"] * 10)
    text = f"4242 (python (worker) x) {fields_after_comm}"
    assert procfs.parse_stat(text) == (4200, 987654)


def test_parse_environ_keeps_only_whitelisted_keys():
    raw = b"MALLOC_ARENA_MAX=2\0MAIBOT_SESSION_TOKEN=secret\0PATH=/usr/bin\0GLIBC_TUNABLES=glibc.malloc.trim_threshold=1\0"
    result = procfs.parse_environ(raw, ["MALLOC_ARENA_MAX", "GLIBC_TUNABLES"])
    assert result == {"MALLOC_ARENA_MAX": "2", "GLIBC_TUNABLES": "glibc.malloc.trim_threshold=1"}


def test_classify_process_uses_exe_name():
    assert procfs.classify_process("python3.13", runner=True) == "plugin_runner"
    assert procfs.classify_process("chromium", runner=False) == "chromium"
    assert procfs.classify_process("python3.12", runner=False) == "python"
    assert procfs.classify_process("node", runner=False) == "node"
    assert procfs.classify_process("git", runner=False) == "git"
    assert procfs.classify_process(None, runner=False) == "unknown"


def test_parse_exe_link_strips_deleted_suffix():
    assert procfs.parse_exe_link("/usr/bin/node (deleted)") == "node"
    assert procfs.parse_exe_link("/usr/lib/chromium/chromium") == "chromium"


def test_parse_net_tcp_returns_all_states_on_port():
    result = procfs.parse_net_tcp(NET_TCP, 19050)
    assert result == [
        {"state": "0A", "remote_port": 0, "tx_queue": 0, "rx_queue": 0, "timer": 0, "inode": 41234},
        {"state": "01", "remote_port": 0xD2F0, "tx_queue": 0x3D6C20, "rx_queue": 0, "timer": 4, "inode": 51234},
        {"state": "01", "remote_port": 0xD2F4, "tx_queue": 0, "rx_queue": 0, "timer": 2, "inode": 51240},
    ]


def test_parse_net_tcp6_reads_port_after_last_colon():
    result = procfs.parse_net_tcp(NET_TCP6, 19050)
    assert result == [{"state": "01", "remote_port": 0xD400, "tx_queue": 0x1000, "rx_queue": 0, "timer": 4, "inode": 61234}]


def test_parse_socket_link():
    assert procfs.parse_socket_link("socket:[51234]") == 51234
    assert procfs.parse_socket_link("pipe:[51234]") is None
    assert procfs.parse_socket_link("/data/MaiBot.db") is None


def test_glibc_heap_estimate_counts_only_aligned_64mib_heaps():
    # 7f2c14001000 起的一对合计正好 64 MiB，但起点没有按 64 MiB 对齐，不是线程堆。
    result = procfs.glibc_heap_estimate(MAPS)
    assert result == {"heaps": 2, "committed_kb": 43140 + 1024, "reserved_kb": 22396 + 64512, "main_heap_kb": 2048}


def test_parse_smaps_reads_each_mapping_and_paths_with_spaces():
    maps = procfs.parse_smaps(SMAPS)
    assert len(maps) == 12
    assert maps[0] == {"start": 0x55D0C0A00000, "end": 0x55D0C0C00000, "perms": "rw-p", "path": "[heap]",
                       "kb": {"Size": 2048, "Rss": 1500, "Anonymous": 1500, "AnonHugePages": 0},
                       "flags": ["rd", "wr", "mr", "mw", "me", "ac"]}
    assert maps[9]["path"] == "/usr/lib/lib foo.so"
    # 3.8 之前的内核没有 VmFlags。
    assert maps[9]["flags"] is None


def test_smaps_headers_give_the_same_heap_estimate_as_maps():
    result = procfs.glibc_heap_estimate(procfs.maps_text_of(SMAPS))
    # 用满 64 MiB、没有保留尾的线程堆，这个估计数不到。
    assert result == {"heaps": 1, "committed_kb": 43140, "reserved_kb": 22396, "main_heap_kb": 2048}


def test_anon_breakdown_separates_heaps_from_standalone_mappings():
    result = procfs.anon_breakdown(procfs.parse_smaps(SMAPS))
    assert result["brk_heap"] == {"maps": 1, "rss_kb": 1500, "anon_kb": 1500, "thp_kb": 0}
    # 带 nr 的是线程堆，包括两个线程堆合并成的 96 MiB 一段；起点对齐、超过 64 MiB 但不带 nr 的不是。
    assert result["glibc_arenas"] == {"maps": 3, "rss_kb": 190000, "anon_kb": 190000, "thp_kb": 6144}
    assert result["other"] == {
        "lt_2m": {"maps": 1, "rss_kb": 300, "anon_kb": 300, "thp_kb": 0},
        "2m_to_64m": {"maps": 1, "rss_kb": 16000, "anon_kb": 16000, "thp_kb": 0},
        "ge_64m": {"maps": 1, "rss_kb": 200000, "anon_kb": 200000, "thp_kb": 8192},
    }
    # 文件映射、保留尾与内核的特殊映射都不计。
    assert list(result["named"]) == ["[anon:cpython:pymalloc]", "[stack]"]
    assert result["named"]["[stack]"] == {"maps": 1, "rss_kb": 100, "anon_kb": 100, "thp_kb": 0}
    assert [(item["kind"], item["size_kb"], item["rss_kb"]) for item in result["largest"]] == [
        ("ge_64m", 262144, 200000), ("glibc_arena", 98304, 90000), ("glibc_arena", 65536, 60000),
        ("glibc_arena", 43140, 40000), ("2m_to_64m", 16384, 16000),
    ]


@pytest.mark.parametrize("flags", [None, "rd wr mr mw me ac sd"])
def test_anon_breakdown_falls_back_to_alignment_without_the_noreserve_flag(flags):
    # 3.8 之前的内核没有 VmFlags；overcommit_memory=2 时线程堆不带 nr。
    text = "7f2c04000000-7f2c06000000 rw-p 00000000 00:00 0\nRss: 500 kB\nAnonymous: 500 kB\n"
    if flags is not None:
        text += f"VmFlags: {flags}\n"
    result = procfs.anon_breakdown(procfs.parse_smaps(text))
    assert result["glibc_arenas"] == {"maps": 1, "rss_kb": 500, "anon_kb": 500, "thp_kb": 0}


def test_anon_breakdown_counts_per_thread_stacks_of_old_kernels_as_one_entry():
    # 4.5 之前的内核把每个线程栈记作 [stack:线程号]。
    text = ("7f0000000000-7f0000800000 rw-p 00000000 00:00 0  [stack:1234]\nRss: 40 kB\nAnonymous: 40 kB\n"
            "7f0000900000-7f0001100000 rw-p 00000000 00:00 0  [stack:1235]\nRss: 60 kB\nAnonymous: 60 kB\n")
    named = procfs.anon_breakdown(procfs.parse_smaps(text))["named"]
    assert named == {"[stack]": {"maps": 2, "rss_kb": 100, "anon_kb": 100, "thp_kb": 0}}


def test_anon_breakdown_folds_extra_named_mappings_into_rest():
    blocks = []
    for index in range(procfs.NAMED_ANON_LIMIT + 2):
        start = 0x7F0000000000 + index * 0x200000
        blocks.append(f"{start:x}-{start + 0x100000:x} rw-p 00000000 00:00 0  [anon:pool{index}]\n"
                      f"Rss: {index + 1} kB\nAnonymous: {index + 1} kB\n")
    named = procfs.anon_breakdown(procfs.parse_smaps("".join(blocks)))["named"]
    assert len(named) == procfs.NAMED_ANON_LIMIT + 1
    # 按常驻量从大到小留前几名，最小的两个并进 _rest。
    assert "[anon:pool11]" in named and "[anon:pool0]" not in named
    assert named["_rest"] == {"maps": 2, "rss_kb": 3, "anon_kb": 3, "thp_kb": 0}


@pytest.mark.parametrize(("text", "mode"), [
    ("always [madvise] never\n", "madvise"),
    ("[always] defer defer+madvise madvise never\n", "always"),
])
def test_parse_thp_mode_reads_the_bracketed_choice(text, mode):
    assert procfs.parse_thp_mode(text) == mode


def test_parse_thp_mode_rejects_unknown_text():
    with pytest.raises(ValueError):
        procfs.parse_thp_mode("always madvise never\n")


def test_thp_settings_reads_enabled_and_defrag(tmp_path, monkeypatch):
    monkeypatch.setattr(procfs, "THP_SYSFS", tmp_path / "missing")
    assert procfs.thp_settings() is None
    monkeypatch.setattr(procfs, "THP_SYSFS", tmp_path)
    (tmp_path / "enabled").write_text("[always] madvise never\n", encoding="utf-8")
    (tmp_path / "defrag").write_text("always defer defer+madvise [madvise] never\n", encoding="utf-8")
    assert procfs.thp_settings() == {"enabled": "always", "defrag": "madvise"}


def test_parse_cgroup_file_v2_and_v1():
    assert procfs.parse_cgroup_file("0::/\n") == ("/", None)
    v1 = "12:memory:/docker/abc\n11:cpu,cpuacct:/docker/abc\n"
    assert procfs.parse_cgroup_file(v1) == (None, "/docker/abc")


def test_parse_limit_and_flat_keyed():
    assert procfs.parse_limit("max\n") is None
    assert procfs.parse_limit("4294967296\n") == 4294967296
    stat = "anon 1048576\nfile 2097152\nkernel 4096\nunrelated 7\n"
    assert procfs.parse_flat_keyed(stat, ("anon", "file", "kernel")) == {"anon": 1048576, "file": 2097152, "kernel": 4096}


MOUNTINFO = (
    "22 1 8:1 / / rw,relatime - ext4 /dev/sda1 rw\n"
    "30 22 0:26 /docker/abc /sys/fs/cgroup/memory ro,nosuid - cgroup cgroup rw,memory\n"
    "31 22 0:27 /docker/abc /sys/fs/cgroup/cpu,cpuacct ro,nosuid - cgroup cgroup rw,cpu,cpuacct\n"
    "32 22 0:28 / /sys/fs/cgroup/unified\\040dir rw - cgroup2 cgroup2 rw\n"
)


def test_parse_mountinfo_cgroups_finds_memory_v1_and_v2_mounts():
    v2, v1 = procfs.parse_mountinfo_cgroups(MOUNTINFO)
    assert v1 == [("/docker/abc", "/sys/fs/cgroup/memory")]
    assert v2 == [("/", "/sys/fs/cgroup/unified dir")]
    assert procfs.parse_mountinfo_cgroups("22 1 8:1 / / rw - ext4 /dev/sda1 rw\n") == ([], [])


def test_parse_mountinfo_cgroups_unescapes_the_mount_root():
    # systemd-nspawn 的机器 scope 名带 \x2d，mountinfo 把其中的反斜杠写成 \134。
    line = "30 22 0:26 /machine.slice/machine-mai\\134x2dbot.scope /sys/fs/cgroup/memory rw - cgroup cgroup rw,memory\n"
    assert procfs.parse_mountinfo_cgroups(line)[1] == [("/machine.slice/machine-mai\\x2dbot.scope", "/sys/fs/cgroup/memory")]


def test_visible_cgroup_dir_prefers_the_later_mount():
    Path = procfs.Path
    # 私有 cgroup 命名空间里原地再挂一层：继承来的那层 root 是 /../..，换算不出目录；后挂的一层可见。
    stacked = [("/../..", "/sys/fs/cgroup"), ("/", "/sys/fs/cgroup")]
    assert procfs.visible_cgroup_dir(stacked, "/") == Path("/sys/fs/cgroup")
    two_visible = [("/", "/first"), ("/", "/second")]
    assert procfs.visible_cgroup_dir(two_visible, "/a") == Path("/second/a")
    assert procfs.visible_cgroup_dir([("/", "/first"), ("/docker/x", "/second")], "/a") == Path("/first/a")
    assert procfs.visible_cgroup_dir([("/docker/x", "/m")], "/a") is None


def test_cgroup_dir_strips_mount_root():
    # cgroup v1 的 Docker 默认共享宿主 cgroup 命名空间：路径就是挂载 root，对应挂载点本身。
    Path = procfs.Path
    assert procfs.cgroup_dir(("/docker/abc", "/sys/fs/cgroup/memory"), "/docker/abc") == Path("/sys/fs/cgroup/memory")
    assert procfs.cgroup_dir(("/docker/abc", "/m"), "/docker/abc/sub") == Path("/m/sub")
    assert procfs.cgroup_dir(("/", "/sys/fs/cgroup"), "/user.slice/a.scope") == Path("/sys/fs/cgroup/user.slice/a.scope")
    assert procfs.cgroup_dir(("/docker/abc", "/m"), "/docker/abcdef") is None
    assert procfs.cgroup_dir(("/docker/abc", "/m"), "/system.slice") is None


# ---- 用临时目录伪造 /proc 与 cgroup 挂载 ------------------------------------------


def stat_line(pid, ppid, starttime=1000):
    return f"{pid} (proc {pid}) S {ppid} " + " ".join(["0"] * 17 + [str(starttime)] + ["0"] * 10)


@pytest.fixture
def fake_proc(tmp_path, monkeypatch):
    root = tmp_path / "proc"
    root.mkdir()
    monkeypatch.setattr(procfs, "PROC", root)

    def add(pid, ppid, cmdline=b"x\0", status=STATUS, **files):
        directory = root / str(pid)
        directory.mkdir()
        (directory / "stat").write_text(stat_line(pid, ppid), encoding="utf-8")
        (directory / "cmdline").write_bytes(cmdline)
        (directory / "status").write_text(status, encoding="utf-8")
        for name, content in files.items():
            (directory / name).write_text(content, encoding="utf-8")
        return directory

    return add


def mount_cgroup(tmp_path, files):
    base = tmp_path / "cgroupfs"
    base.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (base / name).write_text(content, encoding="utf-8")
    return base


def mountinfo_line(fstype, root, mountpoint, superopts):
    return f"30 22 0:26 {root} {mountpoint} rw - {fstype} cgroup {superopts}\n"


def test_descendants_walks_three_levels_and_skips_unrelated(fake_proc, monkeypatch):
    fake_proc(1, 0)
    fake_proc(100, 1)
    fake_proc(101, 100, cmdline=RUNNER_CMDLINE)
    fake_proc(102, 101)
    fake_proc(103, 102)
    fake_proc(200, 1)
    monkeypatch.setattr(procfs, "exe_name", {101: "python3", 102: "node", 103: "chrome"}.get)
    kinds, hidden = procfs.descendants(100)
    assert kinds == {101: "plugin_runner", 102: "node", 103: "chromium"}
    assert hidden == 0


def test_descendants_counts_only_direct_children_as_runners(fake_proc, monkeypatch):
    # Runner 里 fork 出的子进程不 exec，命令行与 Runner 逐字节相同，但它不是 Runner。
    fake_proc(100, 1)
    fake_proc(101, 100, cmdline=RUNNER_CMDLINE)
    fake_proc(102, 101, cmdline=RUNNER_CMDLINE)
    fake_proc(103, 100, cmdline=RUNNER_CMDLINE)
    monkeypatch.setattr(procfs, "exe_name", lambda pid: "python3.12")
    assert procfs.descendants(100)[0] == {101: "plugin_runner", 102: "python", 103: "plugin_runner"}


def test_descendants_never_derives_names_from_rewritten_titles(fake_proc, monkeypatch):
    # npm 改写进程标题后，整条命令行（含密钥）变成一段不含 NUL 的文本；分类结果只能来自可执行文件名。
    title = b"npm exec @smithery/cli@latest run @upstash/context7-mcp --key sk-LIVE-1234abcd" + b"\0" * 20
    fake_proc(100, 1)
    fake_proc(101, 100, cmdline=title)
    fake_proc(102, 100, cmdline=title)
    monkeypatch.setattr(procfs, "exe_name", {101: "node"}.get)
    assert procfs.descendants(100)[0] == {101: "node", 102: "unknown"}


def test_descendants_classifies_processes_without_a_readable_exe_as_unknown(fake_proc, monkeypatch):
    # 伪造的 /proc 里没有 exe 链接，读取报 ENOENT，与僵尸进程相同；另一个进程属于别的用户，读取报 EPERM。
    fake_proc(100, 1)
    fake_proc(101, 100)
    fake_proc(102, 100)
    real_readlink = os.readlink

    def readlink(path, *args, **kwargs):
        if os.path.basename(os.path.dirname(path)) == "102":
            raise PermissionError(13, "Permission denied", str(path))
        return real_readlink(path, *args, **kwargs)

    monkeypatch.setattr(procfs.os, "readlink", readlink)
    assert procfs.exe_name(101) is None and procfs.exe_name(102) is None
    assert procfs.descendants(100)[0] == {101: "unknown", 102: "unknown"}


@pytest.mark.parametrize("gone", [FileNotFoundError(2, "No such file"), ProcessLookupError(3, "No such process")])
def test_descendants_skips_a_runner_that_exits_while_its_cmdline_is_read(fake_proc, monkeypatch, gone):
    # 回收发生在打开之前报 ENOENT（窗口横跨整轮 /proc 扫描，更常见），打开之后才回收报 ESRCH。
    fake_proc(100, 1)
    fake_proc(101, 100, cmdline=RUNNER_CMDLINE)
    fake_proc(102, 100)
    real_read_bytes = procfs.Path.read_bytes

    def read_bytes(self):
        if self.name == "cmdline" and self.parent.name == "101":
            raise gone
        return real_read_bytes(self)

    monkeypatch.setattr(procfs.Path, "read_bytes", read_bytes)
    monkeypatch.setattr(procfs, "exe_name", lambda pid: "git")
    assert procfs.descendants(100)[0] == {102: "git"}


def test_descendants_counts_processes_hidden_by_hidepid(fake_proc, monkeypatch):
    fake_proc(1, 0)
    fake_proc(100, 1)
    fake_proc(101, 100)
    original = procfs.read_text

    def guarded(path):
        if path.parent.name == "1":
            raise PermissionError(13, "Operation not permitted", str(path))
        return original(path)

    monkeypatch.setattr(procfs, "read_text", guarded)
    monkeypatch.setattr(procfs, "exe_name", lambda pid: "git")
    assert procfs.descendants(100) == ({101: "git"}, 1)


def test_anon_bytes_falls_back_to_statm_without_rss_anon(fake_proc, monkeypatch):
    # 4.4 内核：status 没有 RssAnon；statm 的 resident - shared 是匿名页数。
    fake_proc(100, 1, statm="5000 3000 1200 10 0 800 0\n")
    # 页大小必须向系统查：arm64 与 ppc64 的内核常用 64 KiB 页。
    monkeypatch.setattr(procfs.os, "sysconf", {"SC_PAGE_SIZE": 65536}.__getitem__, raising=False)
    assert procfs.anon_bytes(100, {"VmRSS": 12000}) == (3000 - 1200) * 65536
    assert procfs.anon_bytes(100, {"RssAnon": 7}) == 7 * 1024


def test_find_worker_pid_rejects_runner_parent(fake_proc, monkeypatch):
    fake_proc(100, 1, cmdline=b"python\0bot.py\0")
    fake_proc(101, 100, cmdline=RUNNER_CMDLINE)
    monkeypatch.setattr(procfs.os, "getppid", lambda: 100)
    assert procfs.find_worker_pid() == 100
    monkeypatch.setattr(procfs.os, "getppid", lambda: 101)
    with pytest.raises(RuntimeError):
        procfs.find_worker_pid()


def test_fd_summary_counts_types_and_watched_databases(fake_proc, monkeypatch, tmp_path):
    fake_proc(100, 1)
    db = tmp_path / "data" / "MaiBot.db"
    links = {"0": "pipe:[1]", "1": "socket:[2]", "2": "anon_inode:[eventpoll]", "3": str(db), "4": str(db), "5": "/tmp/x"}
    monkeypatch.setattr(procfs.os, "listdir", lambda path: list(links))
    monkeypatch.setattr(procfs.os, "readlink", lambda path: links[os.path.basename(path)])
    assert procfs.fd_summary(100, {"main_db": db}) == {
        "total": 6, "socket": 1, "pipe": 1, "anon_inode": 1, "file": 3, "open_main_db": 2,
    }


def test_webui_connections_keeps_only_worker_owned_established_sockets(fake_proc, monkeypatch):
    directory = fake_proc(100, 1)
    (directory / "net").mkdir()
    (directory / "net" / "tcp").write_text(NET_TCP, encoding="utf-8")
    (directory / "net" / "tcp6").write_text(NET_TCP6, encoding="utf-8")
    # 51240 是同端口上另一个进程的连接，不属于 Worker；41234 是 Worker 的监听套接字，不算连接。
    monkeypatch.setattr(procfs, "socket_inodes", lambda pid: {41234, 51234, 61234})
    result = procfs.webui_connections(100, 19050)
    assert result["listening"] is True
    assert sorted(conn["inode"] for conn in result["conns"]) == [51234, 61234]
    monkeypatch.setattr(procfs, "socket_inodes", lambda pid: {51234})
    assert procfs.webui_connections(100, 19050)["listening"] is False


def test_cgroup_memory_v1_in_docker_with_host_cgroupns(fake_proc, tmp_path):
    base = mount_cgroup(tmp_path, {
        "memory.limit_in_bytes": "9223372036854771712\n", "memory.usage_in_bytes": "1048576\n",
        "memory.max_usage_in_bytes": "2097152\n", "memory.stat": "rss 4096\ncache 8192\n",
        "memory.oom_control": "oom_kill_disable 0\nunder_oom 0\noom_kill 3\n",
    })
    fake_proc(100, 1, cgroup="12:memory:/docker/abc\n0::/\n",
              mountinfo=mountinfo_line("cgroup", "/docker/abc", base, "rw,memory"))
    assert procfs.cgroup_memory(100) == {
        "version": 1, "path": "/docker/abc", "current": 1048576, "peak": 2097152, "max": None,
        "stat": {"rss": 4096, "cache": 8192}, "events": {"oom_kill": 3, "under_oom": 0},
    }


def test_cgroup_memory_v1_with_limit(fake_proc, tmp_path):
    base = mount_cgroup(tmp_path, {
        "memory.limit_in_bytes": "4294967296\n", "memory.usage_in_bytes": "1\n", "memory.max_usage_in_bytes": "2\n",
        "memory.stat": "", "memory.oom_control": "",
    })
    fake_proc(100, 1, cgroup="5:memory:/\n", mountinfo=mountinfo_line("cgroup", "/", base, "rw,memory"))
    assert procfs.cgroup_memory(100)["max"] == 4294967296


def test_cgroup_memory_v2_reads_usage_limit_and_events(fake_proc, tmp_path):
    base = mount_cgroup(tmp_path, {})
    service = base / "system.slice" / "maibot.service"
    service.mkdir(parents=True)
    for name, content in {"memory.current": "1048576\n", "memory.max": "max\n", "memory.peak": "2097152\n",
                          "memory.stat": "anon 4096\nfile 8192\n", "memory.events": "oom 1\noom_kill 1\n"}.items():
        (service / name).write_text(content, encoding="utf-8")
    fake_proc(100, 1, cgroup="0::/system.slice/maibot.service\n", mountinfo=mountinfo_line("cgroup2", "/", base, "rw"))
    assert procfs.cgroup_memory(100) == {
        "version": 2, "path": "/system.slice/maibot.service", "current": 1048576, "max": None, "peak": 2097152,
        "stat": {"anon": 4096, "file": 8192}, "events": {"oom": 1, "oom_kill": 1},
    }


def test_cgroup_memory_v2_without_memory_controller_or_at_root(fake_proc, tmp_path):
    # 树莓派默认内核禁用 memcg：cgroup 目录在，但没有任何 memory.* 文件；根 cgroup 上也没有 memory.current。
    base = mount_cgroup(tmp_path, {"memory.stat": "anon 1\n"})
    (base / "user.slice").mkdir()
    fake_proc(100, 1, cgroup="0::/user.slice\n", mountinfo=mountinfo_line("cgroup2", "/", base, "rw"))
    fake_proc(101, 1, cgroup="0::/\n", mountinfo=mountinfo_line("cgroup2", "/", base, "rw"))
    for pid in (100, 101):
        result = procfs.cgroup_memory(pid)
        assert result["version"] == 2 and "current" not in result
        assert "memory 控制器" in result["unavailable"]


def test_cgroup_memory_reports_invisible_directory_and_missing_mounts(fake_proc, tmp_path):
    base = mount_cgroup(tmp_path, {})
    fake_proc(100, 1, cgroup="12:memory:/system.slice/x\n",
              mountinfo=mountinfo_line("cgroup", "/docker/abc", base, "rw,memory"))
    assert procfs.cgroup_memory(100)["unavailable"] == "cgroup 目录在本挂载命名空间里不可见"
    fake_proc(101, 1, cgroup="0::/\n", mountinfo="22 1 8:1 / / rw - ext4 /dev/sda1 rw\n")
    assert procfs.cgroup_memory(101) == {"version": None}


def test_cgroup_memory_prefers_v1_in_hybrid_mode(fake_proc, tmp_path):
    # 混合模式：memory 控制器挂在 v1，统一层级里没有它；先看 v2 会读到一个没有 memory.* 的目录。
    v1_base = mount_cgroup(tmp_path / "v1", {
        "memory.limit_in_bytes": "4294967296\n", "memory.usage_in_bytes": "7\n", "memory.max_usage_in_bytes": "8\n",
        "memory.stat": "", "memory.oom_control": "",
    })
    v2_base = mount_cgroup(tmp_path / "v2", {})
    (v2_base / "user.slice").mkdir()
    fake_proc(100, 1, cgroup="12:memory:/user.slice\n0::/user.slice\n",
              mountinfo=mountinfo_line("cgroup", "/user.slice", v1_base, "rw,memory")
              + mountinfo_line("cgroup2", "/", v2_base, "rw"))
    result = procfs.cgroup_memory(100)
    assert (result["version"], result["current"], result["max"]) == (1, 7, 4294967296)


def test_cgroup_memory_v2_without_memory_peak(fake_proc, tmp_path):
    # memory.peak 从 5.19 起才有（Debian 11、Ubuntu 22.04 的内核都没有）。
    base = mount_cgroup(tmp_path, {"memory.current": "5\n", "memory.max": "100\n", "memory.stat": "", "memory.events": ""})
    fake_proc(100, 1, cgroup="0::/\n", mountinfo=mountinfo_line("cgroup2", "/", base, "rw"))
    assert procfs.cgroup_memory(100) == {"version": 2, "path": "/", "current": 5, "max": 100, "stat": {}, "events": {}}


def test_cgroup_memory_v2_reports_invisible_directory(fake_proc, tmp_path):
    base = mount_cgroup(tmp_path, {"memory.current": "5\n"})
    fake_proc(100, 1, cgroup="0::/system.slice\n", mountinfo=mountinfo_line("cgroup2", "/docker/abc", base, "rw"))
    assert procfs.cgroup_memory(100) == {
        "version": 2, "path": "/system.slice", "unavailable": "cgroup 目录在本挂载命名空间里不可见",
    }


def test_cgroup_memory_v1_without_memory_files(fake_proc, tmp_path):
    base = mount_cgroup(tmp_path, {"memory.usage_in_bytes": "1\n"})
    fake_proc(100, 1, cgroup="12:memory:/\n", mountinfo=mountinfo_line("cgroup", "/", base, "rw,memory"))
    assert procfs.cgroup_memory(100)["unavailable"] == "cgroup 目录在本挂载命名空间里不可见"


def test_cgroup_memory_matches_an_escaped_mount_root(fake_proc, tmp_path):
    base = mount_cgroup(tmp_path, {
        "memory.limit_in_bytes": "4294967296\n", "memory.usage_in_bytes": "3\n", "memory.max_usage_in_bytes": "4\n",
        "memory.stat": "", "memory.oom_control": "",
    })
    # systemd 的 scope 名里有字面的反斜杠（\x2d），mountinfo 把反斜杠写成八进制转义，/proc/<pid>/cgroup 不转义。
    backslash = chr(92)
    scope = f"/machine.slice/machine-mai{backslash}x2dbot.scope"
    fake_proc(100, 1, cgroup=f"12:memory:{scope}\n",
              mountinfo=mountinfo_line("cgroup", scope.replace(backslash, f"{backslash}134"), base, "rw,memory"))
    assert procfs.cgroup_memory(100)["current"] == 3


def test_cgroup_memory_reads_the_visible_one_of_stacked_mounts(fake_proc, tmp_path):
    base = mount_cgroup(tmp_path, {"memory.current": "9\n", "memory.max": "max\n", "memory.stat": "", "memory.events": ""})
    fake_proc(100, 1, cgroup="0::/\n",
              mountinfo=mountinfo_line("cgroup2", "/../..", base, "rw") + mountinfo_line("cgroup2", "/", base, "rw"))
    assert procfs.cgroup_memory(100)["current"] == 9


def test_cgroup_memory_v1_reads_the_visible_one_of_stacked_mounts(fake_proc, tmp_path):
    base = mount_cgroup(tmp_path, {
        "memory.limit_in_bytes": "4294967296\n", "memory.usage_in_bytes": "7\n", "memory.max_usage_in_bytes": "8\n",
        "memory.stat": "", "memory.oom_control": "",
    })
    fake_proc(100, 1, cgroup="12:memory:/\n",
              mountinfo=mountinfo_line("cgroup", "/../..", base, "rw,memory") + mountinfo_line("cgroup", "/", base, "rw,memory"))
    assert procfs.cgroup_memory(100)["current"] == 7
