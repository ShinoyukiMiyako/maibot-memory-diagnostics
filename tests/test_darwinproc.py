import ctypes
import errno
import os
import shutil
import subprocess
import sys
import threading
import time

import pytest

from memdiag import darwinproc

darwin_only = pytest.mark.skipif(sys.platform != "darwin", reason="只在 macOS 上有意义")
needs_cc = pytest.mark.skipif(shutil.which("cc") is None, reason="对照系统头文件需要 C 编译器")

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIB = 1024 * 1024
LAYOUT_HEADERS = "#include <libproc.h>\n#include <stddef.h>\n#include <stdio.h>\n#include <sys/proc_info.h>\n#include <sys/resource.h>\n"
STRUCTURES = [darwinproc.proc_bsdinfo, darwinproc.proc_taskinfo, darwinproc.rusage_info_v4]


def run_c_program(tmp_path, body):
    source = tmp_path / "probe.c"
    source.write_text(f"{LAYOUT_HEADERS}int main(void) {{\n{body}\nreturn 0;\n}}\n", encoding="utf-8")
    binary = tmp_path / "probe"
    subprocess.run(["cc", str(source), "-o", str(binary)], check=True, capture_output=True, text=True)
    return subprocess.run([str(binary)], check=True, capture_output=True, text=True).stdout


def test_struct_sizes_match_the_kernel_layout():
    # 对照系统头文件的严格校验只能在 macOS 上跑；这里保证在别的平台改结构体时也会挂。
    assert ctypes.sizeof(darwinproc.proc_bsdinfo) == 136
    assert ctypes.sizeof(darwinproc.proc_taskinfo) == 96
    assert ctypes.sizeof(darwinproc.rusage_info_v4) == 296
    assert darwinproc.rusage_info_v4.ri_resident_size.offset == 64
    assert darwinproc.rusage_info_v4.ri_phys_footprint.offset == 72
    assert darwinproc.rusage_info_v4.ri_lifetime_max_phys_footprint.offset == 240


GONE = ProcessLookupError(3, "No such process")


def fake_tree(monkeypatch, tree, names, failure=GONE):
    def child_pids(pid):
        return tree.get(pid, [])

    def process_name(pid):
        if pid not in names:
            raise failure
        return names[pid]

    monkeypatch.setattr(darwinproc, "child_pids", child_pids)
    monkeypatch.setattr(darwinproc, "process_name", process_name)


def test_descendants_walks_the_whole_tree(monkeypatch):
    fake_tree(monkeypatch, {10: [11, 12], 11: [13], 13: [14]},
              {11: "Python", 12: "node", 13: "Google Chrome Helper", 14: "crashpad_handler"})
    assert darwinproc.descendants(10) == {11: "Python", 12: "node", 13: "Google Chrome Helper", 14: "crashpad_handler"}


@pytest.mark.parametrize("failure", [ProcessLookupError(3, "No such process"), PermissionError(1, "Operation not permitted")])
def test_descendants_skips_processes_it_cannot_read(monkeypatch, failure):
    # 12 已退出或属于别的用户：不计入，也不再往下走。
    fake_tree(monkeypatch, {10: [11, 12], 12: [13]}, {11: "node", 13: "orphan"}, failure)
    assert darwinproc.descendants(10) == {11: "node"}


class FakeLibproc:
    """按真实 libproc 的约定：proc_listpids 返回字节数，出错时返回 0 并设 errno；proc_pid_rusage 出错返回 -1。"""

    def __init__(self, pids=(), failing_errno=0, fail_at_call=0, pidinfo=0, comm=b"", name=b""):
        self.pids = pids
        self.failing_errno = failing_errno
        self.fail_at_call = fail_at_call
        self.calls = 0
        self.pidinfo = pidinfo
        self.comm = comm
        self.name = name

    def proc_listpids(self, kind, value, buffer, size):
        assert kind == darwinproc.PROC_PPID_ONLY
        self.calls += 1
        if self.failing_errno and self.calls == self.fail_at_call:
            ctypes.set_errno(self.failing_errno)
            return 0
        if buffer is None:
            return (len(self.pids) + 1) * 4
        for index, pid in enumerate(self.pids):
            buffer[index] = pid
        return len(self.pids) * 4

    def proc_pidinfo(self, pid, flavor, arg, buffer, size):
        ctypes.set_errno(self.failing_errno)
        if isinstance(buffer._obj, darwinproc.proc_bsdinfo):
            buffer._obj.pbi_comm = self.comm
            buffer._obj.pbi_name = self.name
        return self.pidinfo

    def proc_pid_rusage(self, pid, flavor, buffer):
        ctypes.set_errno(self.failing_errno)
        return -1 if self.failing_errno else 0


def use_libproc(monkeypatch, **values):
    fake = FakeLibproc(**values)
    monkeypatch.setattr(darwinproc, "_libproc", lambda: fake)


def test_child_pids_returns_what_the_kernel_wrote(monkeypatch):
    use_libproc(monkeypatch, pids=[11, 12, 13])
    assert darwinproc.child_pids(10) == [11, 12, 13]


def test_child_pids_treats_zero_without_errno_as_no_children(monkeypatch):
    use_libproc(monkeypatch)
    assert darwinproc.child_pids(10) == []


@pytest.mark.parametrize("fail_at_call", [1, 2])
def test_child_pids_reports_failures_instead_of_no_children(monkeypatch, fail_at_call):
    # libproc 出错时也返回 0；当成“没有子进程”的话，子进程内存会悄悄从样本里消失。
    use_libproc(monkeypatch, pids=[11], failing_errno=errno.EINVAL, fail_at_call=fail_at_call)
    with pytest.raises(OSError) as caught:
        darwinproc.child_pids(10)
    assert caught.value.errno == errno.EINVAL


@pytest.mark.parametrize(("code", "expected"), [(0, ProcessLookupError), (errno.ESRCH, ProcessLookupError), (errno.EPERM, PermissionError)])
def test_pidinfo_failures_become_the_matching_exception(monkeypatch, code, expected):
    # 进程已退出时 proc_pidinfo 可能返回 0 而不设 errno，这也要算进程不存在。
    use_libproc(monkeypatch, failing_errno=code)
    with pytest.raises(expected):
        darwinproc.process_created(10)


def test_pidinfo_refuses_a_short_read(monkeypatch):
    # 写入的字节数不对说明结构体与系统不符，继续读字段就是读错位的数据。
    use_libproc(monkeypatch, pidinfo=8)
    with pytest.raises(RuntimeError):
        darwinproc.process_threads(10)


def test_rusage_failure_becomes_the_matching_exception(monkeypatch):
    use_libproc(monkeypatch, failing_errno=errno.EPERM)
    with pytest.raises(PermissionError):
        darwinproc.process_memory(10)


def test_process_name_prefers_the_full_name_and_falls_back_to_comm(monkeypatch):
    size = ctypes.sizeof(darwinproc.proc_bsdinfo)
    use_libproc(monkeypatch, pidinfo=size, comm=b"Google Chrome H", name=b"Google Chrome Helper")
    assert darwinproc.process_name(10) == "Google Chrome Helper"
    use_libproc(monkeypatch, pidinfo=size, comm=b"sleep", name=b"")
    assert darwinproc.process_name(10) == "sleep"


# ---- 真实 libproc ----------------------------------------------------------------


@darwin_only
@needs_cc
def test_constants_match_the_system_headers(tmp_path):
    output = run_c_program(tmp_path, r'printf("%d %d %d %d %d\n", PROC_PIDTBSDINFO, PROC_PIDTASKINFO, PROC_PPID_ONLY, RUSAGE_INFO_V4, MAXCOMLEN);')
    assert output.split() == [str(value) for value in (
        darwinproc.PROC_PIDTBSDINFO, darwinproc.PROC_PIDTASKINFO, darwinproc.PROC_PPID_ONLY,
        darwinproc.RUSAGE_INFO_V4, darwinproc.MAXCOMLEN,
    )]


@darwin_only
@needs_cc
@pytest.mark.parametrize("structure", STRUCTURES, ids=lambda structure: structure.__name__)
def test_struct_layout_matches_the_system_headers(tmp_path, structure):
    name = structure.__name__
    fields = [field for field, _ in structure._fields_]
    lines = [rf'printf("sizeof %zu\n", sizeof(struct {name}));']
    lines += [rf'printf("{field} %zu\n", offsetof(struct {name}, {field}));' for field in fields]
    output = run_c_program(tmp_path, "\n".join(lines))
    measured = {key: int(value) for key, value in (line.split() for line in output.splitlines())}
    expected = {field: getattr(structure, field).offset for field in fields}
    assert measured == {"sizeof": ctypes.sizeof(structure), **expected}


@darwin_only
def test_process_memory_reports_positive_counters_for_self():
    memory = darwinproc.process_memory(os.getpid())
    assert memory["resident"] > 0
    assert memory["footprint"] > 0
    assert memory["peak_footprint"] >= memory["footprint"]


@darwin_only
def test_footprint_follows_anonymous_allocations():
    # 主指标的意义全在这里：匿名内存涨了，它要跟着涨。
    before = darwinproc.process_memory(os.getpid())["footprint"]
    block = bytearray(256 * MIB)
    for offset in range(0, len(block), 4096):
        block[offset] = 1
    after = darwinproc.process_memory(os.getpid())
    del block
    assert after["footprint"] - before > 200 * MIB
    assert after["peak_footprint"] >= after["footprint"]


@darwin_only
def test_process_threads_counts_new_threads():
    before = darwinproc.process_threads(os.getpid())
    release = threading.Event()
    workers = [threading.Thread(target=release.wait) for _ in range(5)]
    for worker in workers:
        worker.start()
    try:
        assert darwinproc.process_threads(os.getpid()) >= before + 5
    finally:
        release.set()
        for worker in workers:
            worker.join()


@darwin_only
def test_process_created_is_stable_and_distinguishes_processes():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        own, other = darwinproc.process_created(os.getpid()), darwinproc.process_created(child.pid)
        assert own > 0 and other > own
        assert darwinproc.process_created(os.getpid()) == own
    finally:
        child.kill()
        child.wait()


@darwin_only
def test_exited_process_raises_process_lookup_error():
    child = subprocess.Popen(["/bin/sleep", "0"])
    child.wait()
    for call in (darwinproc.process_memory, darwinproc.process_threads, darwinproc.process_created, darwinproc.process_name):
        with pytest.raises(ProcessLookupError):
            call(child.pid)


@darwin_only
def test_zombie_children_are_left_out_of_descendants():
    # proc_listpids 会列出僵尸进程，内存读数也不报错，只有名字和线程数读不到。
    child = subprocess.Popen(["/bin/sleep", "0"])
    try:
        deadline = time.monotonic() + 5
        while child.pid not in darwinproc.child_pids(os.getpid()) or child.pid in darwinproc.descendants(os.getpid()):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert darwinproc.process_memory(child.pid)["footprint"] == 0
        with pytest.raises(ProcessLookupError):
            darwinproc.process_threads(child.pid)
    finally:
        child.wait()


@darwin_only
@pytest.mark.skipif(sys.platform == "darwin" and os.geteuid() == 0, reason="root 能读所有进程")
def test_processes_of_other_users_raise_permission_error():
    for call in (darwinproc.process_memory, darwinproc.process_threads, darwinproc.process_created, darwinproc.process_name):
        with pytest.raises(PermissionError):
            call(1)


@darwin_only
def test_find_worker_pid_in_child_of_a_venv_python(tmp_path):
    # Runner 由 sys.executable 拉起：venv 与框架版 Python 都不能在中间多出一个进程，否则 Worker 认错。
    subprocess.run([sys.executable, "-m", "venv", str(tmp_path / "venv")], check=True)
    for python in (sys.executable, tmp_path / "venv" / "bin" / "python"):
        completed = subprocess.run(
            [str(python), "-c", "import darwinproc; print(darwinproc.find_worker_pid())"],
            cwd=PLUGIN_DIR, capture_output=True, text=True, check=True,
        )
        assert int(completed.stdout.strip()) == os.getpid()


@darwin_only
def test_descendants_includes_grandchildren():
    script = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen(['/bin/sleep', '30'])\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(30)\n"
    )
    child = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    try:
        grandchild = int(child.stdout.readline())
        found = darwinproc.descendants(os.getpid())
        assert child.pid in found
        assert found[grandchild] == "sleep"
    finally:
        subprocess.run(["pkill", "-9", "-P", str(child.pid)], check=False)
        child.kill()
        child.wait()


@darwin_only
def test_long_executable_names_are_not_cut_to_the_comm_limit(tmp_path):
    # Chromium 的辅助进程名都很长，按 15 字节的 pbi_comm 汇总会把不同种类并成一桶。
    name = "Google Chrome Helper (Renderer)"
    copied = tmp_path / name
    shutil.copy("/bin/sleep", copied)
    child = subprocess.Popen([str(copied), "30"])
    try:
        deadline = time.monotonic() + 5
        while darwinproc.process_name(child.pid) != name[:32] and time.monotonic() < deadline:
            time.sleep(0.01)
        assert darwinproc.process_name(child.pid) == name[:32]
    finally:
        child.kill()
        child.wait()
