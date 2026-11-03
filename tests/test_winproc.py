import gc
import importlib.util
import os
import subprocess
import sys
import weakref

import pytest

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="只在 Windows 上有意义")

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def winproc_module():
    # winproc 只在函数体里调用 Windows API；ctypes.wintypes 在 Linux 上也能导入，纯逻辑部分所有平台都能测。
    from memdiag import winproc

    return winproc


def test_resolve_worker_returns_parent_when_it_is_not_a_launcher():
    table = {30: (20, "python.exe", 1), 20: (10, "python.exe", 1)}
    assert winproc_module().resolve_worker(table, 30, lambda pid: False) == 20


def test_resolve_worker_skips_one_launcher():
    table = {30: (25, "python.exe", 1), 25: (20, "python.exe", 1), 20: (10, "python.exe", 1)}
    assert winproc_module().resolve_worker(table, 30, lambda pid: pid == 25) == 20


def test_resolve_worker_rejects_two_launchers_in_a_row():
    table = {30: (25, "python.exe", 1), 25: (24, "python.exe", 1), 24: (20, "python.exe", 1)}
    with pytest.raises(RuntimeError):
        winproc_module().resolve_worker(table, 30, lambda pid: pid in (25, 24))


def test_is_venv_launcher_compares_file_identity_with_the_running_image(tmp_path):
    winproc = winproc_module()
    launcher, base = tmp_path / "launcher.exe", tmp_path / "base.exe"
    launcher.write_bytes(b"launcher")
    base.write_bytes(b"base")
    # 硬链接模拟经目录联接访问到的同一个文件：路径字符串不同，文件相同。
    via_junction = tmp_path / "via_junction.exe"
    os.link(launcher, via_junction)
    # 启动器式 venv：sys.executable 是启动器，本进程真正运行的映像是基础解释器。
    assert winproc.is_venv_launcher(str(via_junction), str(launcher), str(base))
    assert winproc.is_venv_launcher(str(launcher), str(launcher), str(base))
    assert not winproc.is_venv_launcher(str(base), str(launcher), str(base))
    # 系统 Python：sys.executable 就是本进程的映像，Worker 的映像与它相同但不是启动器。
    assert not winproc.is_venv_launcher(str(base), str(base), str(base))
    # --symlinks 建的 venv 或经目录联接安装的 Python：sys.executable 是另一条路径，指向同一个文件。
    symlinked = tmp_path / "venv_python.exe"
    os.link(base, symlinked)
    assert not winproc.is_venv_launcher(str(base), str(symlinked), str(base))
    assert not winproc.is_venv_launcher(str(base), str(symlinked), str(symlinked))


def layout(tmp_path, monkeypatch, images, executable, base_executable):
    """按给定的各进程映像伪造进程表：30 是 Runner，往上依次是 25（有启动器时）、20（Worker）、10（守护进程）。"""
    winproc = winproc_module()
    parents = {30: 25 if 25 in images else 20, 25: 20, 20: 10, 10: 1}
    table = {pid: (parents[pid], "python.exe", 1) for pid in images}
    monkeypatch.setattr(winproc, "process_table", lambda: table)
    monkeypatch.setattr(winproc, "process_image", lambda pid: str(images[pid]))
    monkeypatch.setattr(winproc.sys, "executable", str(executable))
    monkeypatch.setattr(winproc.sys, "_base_executable", str(base_executable))
    return winproc.find_worker_pid(30)


def test_find_worker_pid_on_each_interpreter_layout(tmp_path, monkeypatch):
    install = tmp_path / "Python312" / "python.exe"
    install.parent.mkdir()
    install.write_bytes(b"python")
    launcher = tmp_path / "venv" / "Scripts" / "python.exe"
    launcher.parent.mkdir(parents=True)
    launcher.write_bytes(b"venvlauncher")
    copy = tmp_path / "copyvenv" / "Scripts" / "python.exe"
    copy.parent.mkdir(parents=True)
    copy.write_bytes(b"python copy")
    junction = tmp_path / "via_junction.exe"
    os.link(install, junction)
    # 启动器式 venv：每层都是 启动器 -> 基础解释器，Runner 的父进程是启动器。
    assert layout(tmp_path, monkeypatch, {30: install, 25: launcher, 20: install, 10: install}, launcher, install) == 20
    # 解释器副本式 venv：没有启动器，三层映像都是副本；sys._base_executable 却指向安装目录。
    assert layout(tmp_path, monkeypatch, {30: copy, 20: copy, 10: copy}, copy, install) == 20
    # 经目录联接安装的系统 Python：sys.executable 是联接路径，进程映像是解析后的真实路径。
    assert layout(tmp_path, monkeypatch, {30: install, 20: install, 10: install}, junction, junction) == 20


def test_descendants_rejects_reused_parent_pids_and_cycles(monkeypatch):
    winproc = winproc_module()
    table = {
        # 根 10 的父 pid 13 是复用了旧号的孙进程，进程表里因此出现环。
        10: (13, "python.exe", 1),
        11: (10, "python.exe", 1),
        12: (10, "explorer.exe", 1),  # 原父进程 10 早已退出留下的孤儿，比现在的 10 更早创建
        13: (11, "node.exe", 1),
        14: (11, "chrome.exe", 1),  # 打不开
        15: (12, "notepad.exe", 1),
    }
    created = {10: 100, 11: 150, 12: 50, 13: 160, 15: 60}

    def process_created(pid):
        if pid not in created:
            raise OSError(5, "Access is denied")
        return created[pid]

    monkeypatch.setattr(winproc, "process_table", lambda: table)
    monkeypatch.setattr(winproc, "process_created", process_created)
    assert winproc.descendants(10) == {11: "python.exe", 13: "node.exe"}


def test_descendants_visits_each_process_once_even_with_equal_creation_times(monkeypatch):
    # 创建时间相同（时钟粒度内先后创建）的环，只靠创建时间挡不住，要靠已访问集合。
    winproc = winproc_module()
    table = {10: (11, "python.exe", 1), 11: (10, "node.exe", 1)}
    calls = []

    def process_created(pid):
        calls.append(pid)
        if len(calls) > 50:
            raise AssertionError("进程树遍历没有终止")
        return 100

    monkeypatch.setattr(winproc, "process_table", lambda: table)
    monkeypatch.setattr(winproc, "process_created", process_created)
    assert winproc.descendants(10) == {11: "node.exe"}


@windows_only
def test_process_memory_reports_positive_counters_for_self():
    winproc = winproc_module()
    result = winproc.process_memory(os.getpid())
    assert result["working_set"] > 0
    assert result["private_usage"] > 0
    assert result["peak_working_set"] >= result["working_set"]
    assert result["handle_count"] > 0


@windows_only
def test_reloaded_module_releases_its_structure_classes():
    # 宿主重载插件时把插件模块移出 sys.modules 再重新执行，每次都会生成新的结构体类；旧类必须能被回收。
    spec = importlib.util.spec_from_file_location("memdiag_winproc_reload_probe", os.path.join(PLUGIN_DIR, "winproc.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.process_memory(os.getpid())
    module.process_table()
    classes = [weakref.ref(module.PROCESS_MEMORY_COUNTERS_EX), weakref.ref(module.PROCESSENTRY32W)]
    del module, spec
    gc.collect()
    assert [ref() for ref in classes] == [None, None]


@windows_only
def test_process_created_distinguishes_processes():
    winproc = winproc_module()
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        own, other = winproc.process_created(os.getpid()), winproc.process_created(child.pid)
        assert own > 0 and other > own
    finally:
        child.kill()
        child.wait()


@windows_only
def test_find_worker_pid_skips_venv_launcher_in_child():
    # 子进程用与 Worker 拉起 Runner 相同的方式启动（sys.executable）；venv 下中间隔着启动器，结果仍应是本进程。
    completed = subprocess.run(
        [sys.executable, "-c", "import winproc; print(winproc.find_worker_pid())"],
        cwd=PLUGIN_DIR, capture_output=True, text=True, check=True,
    )
    assert int(completed.stdout.strip()) == os.getpid()


@windows_only
def test_descendants_includes_grandchildren():
    winproc = winproc_module()
    script = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(30)\n"
    )
    child = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    try:
        grandchild = int(child.stdout.readline())
        found = winproc.descendants(os.getpid())
        assert child.pid in found and grandchild in found
    finally:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(child.pid)], capture_output=True, check=False)
        child.wait()
