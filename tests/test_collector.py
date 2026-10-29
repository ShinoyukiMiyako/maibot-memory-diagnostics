import json
import os
import sqlite3
import sys

import pytest

from memdiag import collector, procfs
from memdiag.collector import (
    Collector, Targets, Window, WorkerGone, dir_stats, file_stats, identity_of, sidecar_files, summarize_connections,
)


def fast_sample(primary, rss, hwm, rpc_ms=None, webui=None):
    sample = {"worker": {"primary": primary, "rss": rss, "hwm": hwm}, "rpc_ms": rpc_ms}
    if webui is not None:
        sample["webui"] = webui
    return sample


def test_window_tracks_extremes_and_last_values():
    window = Window()
    window.add(fast_sample(100, 150, 200, rpc_ms=3.0, webui={"tx_max": 10, "stalled": 0}))
    window.add(fast_sample(300, 350, 400, rpc_ms=12.5, webui={"tx_max": 5_000_000, "stalled": 1}))
    window.add(fast_sample(200, 250, 400, rpc_ms=None))
    assert window.as_record() == {
        "samples": 3,
        "primary_min": 100,
        "primary_max": 300,
        "primary_last": 200,
        "rss_max": 350,
        "hwm_last": 400,
        "rpc_ms_max": 12.5,
        "webui_tx_max": 5_000_000,
        "webui_stalled_max": 1,
    }


def test_window_keeps_webui_unmeasured_as_none():
    # Windows 与未启用 WebUI 时样本里没有 webui，窗口不能记成“测得 0”。
    window = Window()
    window.add(fast_sample(1, 1, 1))
    record = window.as_record()
    assert record["webui_tx_max"] is None and record["webui_stalled_max"] is None
    window.add(fast_sample(1, 1, 1, webui={"tx_max": 0, "stalled": 0}))
    assert window.as_record()["webui_tx_max"] == 0


def test_summarize_connections_flags_zero_window_with_large_queue_only():
    connections = [
        {"state": "01", "remote_port": 1, "tx_queue": 4_000_000, "rx_queue": 0, "timer": 4, "inode": 1},
        {"state": "01", "remote_port": 2, "tx_queue": 4_000_000, "rx_queue": 0, "timer": 1, "inode": 2},
        {"state": "01", "remote_port": 3, "tx_queue": 100, "rx_queue": 0, "timer": 4, "inode": 3},
    ]
    assert summarize_connections({"listening": True, "conns": connections}) == {
        "listening": True, "conns": 3, "stalled": 1, "tx_max": 4_000_000, "tx_sum": 8_000_100,
    }
    assert summarize_connections({"listening": False, "conns": []}) == {
        "listening": False, "conns": 0, "stalled": 0, "tx_max": 0, "tx_sum": 0,
    }


def test_file_stats_marks_missing_files_as_none(tmp_path):
    present = tmp_path / "review_logs.json"
    present.write_bytes(b"x" * 1234)
    result = file_stats({"review_log": present, "memory_jsonl": tmp_path / "missing.jsonl"})
    assert result["review_log"]["size"] == 1234
    assert result["memory_jsonl"] is None


def test_sidecar_files_derive_wal_and_shm_names(tmp_path):
    db = tmp_path / "data" / "MaiBot.db"
    assert sidecar_files({"main_db": db}) == {
        "main_db_wal": tmp_path / "data" / "MaiBot.db-wal",
        "main_db_shm": tmp_path / "data" / "MaiBot.db-shm",
    }


def test_dir_stats_counts_matching_files_only(tmp_path):
    (tmp_path / "app_20261029_000000.log.jsonl").write_bytes(b"x" * 10)
    (tmp_path / "app_20261029_000100.log.jsonl").write_bytes(b"x" * 5)
    (tmp_path / "other.txt").write_bytes(b"x" * 99)
    assert dir_stats(tmp_path, "app_*.log.jsonl") == {"files": 2, "bytes": 15}
    assert dir_stats(tmp_path / "missing", "*") is None


SUPPORTED = pytest.mark.skipif(not (sys.platform == "win32" or sys.platform.startswith("linux")), reason="需要 Linux 或 Windows")
LINUX_ONLY = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="只在 Linux 上有意义")
WINDOWS_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="只在 Windows 上有意义")


def make_targets(tmp_path, **overrides):
    pid = os.getpid()
    values = dict(worker_pid=pid, worker_identity=(pid, 0), instance_root=tmp_path, webui_port=None,
                  watched_files={}, db_files={})
    values.update(overrides)
    return Targets(**values)


def as_runner_child(monkeypatch):
    # Linux 上 check_worker 还会比对 getppid；让测试进程扮演“自己的 Runner”，被测的 Worker 就是测试进程本身。
    monkeypatch.setattr(collector.os, "getppid", lambda: os.getpid())


# ---- 用替身 procfs 测 Linux 快速样本的分支（任何平台都能跑） ------------------------

FULL = {"VmRSS": 1000, "VmHWM": 2000, "RssAnon": 800, "VmSwap": 0, "Threads": 9}


@pytest.fixture
def linux_fast(tmp_path, monkeypatch):
    monkeypatch.setattr(collector, "IS_LINUX", True)
    snapshots = {}

    def process_snapshot(pid):
        if pid not in snapshots:
            raise FileNotFoundError(2, "No such file or directory", f"/proc/{pid}/status")
        return dict(snapshots[pid])

    monkeypatch.setattr(procfs, "process_snapshot", process_snapshot)
    monkeypatch.setattr(procfs, "anon_bytes", lambda pid, status: status["RssAnon"] * 1024)
    sampler = Collector(make_targets(tmp_path, worker_pid=100))
    monkeypatch.setattr(sampler, "check_worker", lambda: None)
    snapshots[100] = FULL
    snapshots[os.getpid()] = {"VmRSS": 50, "RssAnon": 40}
    return sampler, snapshots, monkeypatch


def test_fast_linux_counts_zombie_children_separately(linux_fast):
    sampler, snapshots, monkeypatch = linux_fast
    # 真实的僵尸进程读不到 exe（ENOENT），descendants 把它们归为 unknown；这个桶里没有活进程。
    snapshots[11] = {"Threads": 1}
    snapshots[14] = {"Threads": 1}
    snapshots[12] = {"VmRSS": 300, "RssAnon": 200}
    snapshots[13] = {"VmRSS": 100, "RssAnon": 50}
    snapshots[15] = {"Threads": 1}
    kinds = {11: "unknown", 14: "unknown", 12: "node", 13: "chromium", 15: "chromium"}
    monkeypatch.setattr(procfs, "descendants", lambda pid: (kinds, 2))
    sample = sampler.fast()
    assert sample["worker"] == {"rss": 1000 * 1024, "primary": 800 * 1024, "hwm": 2000 * 1024, "swap": 0, "threads": 9}
    assert sample["children"] == {
        "unknown": {"n": 0, "rss": 0, "anon": 0, "exiting": 2},
        "node": {"n": 1, "rss": 300 * 1024, "anon": 200 * 1024},
        "chromium": {"n": 1, "rss": 100 * 1024, "anon": 50 * 1024, "exiting": 1},
    }
    assert sample["proc_hidden"] == 2
    assert sample["self"] == {"rss": 50 * 1024, "anon": 40 * 1024}
    assert "rpc_ms" not in sample and "webui" not in sample


def test_fast_linux_skips_children_that_vanish(linux_fast):
    sampler, snapshots, monkeypatch = linux_fast
    monkeypatch.setattr(procfs, "descendants", lambda pid: ({11: "node"}, 0))
    sample = sampler.fast()
    assert sample["children"] == {}
    assert "proc_hidden" not in sample


def test_fast_linux_treats_worker_without_address_space_as_gone(linux_fast):
    sampler, snapshots, monkeypatch = linux_fast
    snapshots[100] = {"Threads": 1}
    with pytest.raises(WorkerGone):
        sampler.fast()


def test_fast_linux_adds_webui_summary_when_port_known(linux_fast):
    sampler, snapshots, monkeypatch = linux_fast
    sampler.targets.webui_port = 8001
    monkeypatch.setattr(procfs, "descendants", lambda pid: ({}, 0))
    conn = {"state": "01", "remote_port": 1, "tx_queue": 4_000_000, "rx_queue": 0, "timer": 4, "inode": 1}
    monkeypatch.setattr(procfs, "webui_connections", lambda pid, port: {"listening": True, "conns": [conn]})
    assert sampler.fast()["webui"]["stalled"] == 1


@pytest.fixture
def linux_trend(tmp_path, monkeypatch):
    """在伪造的 /proc 上跑 Linux 趋势记录（任何平台都能跑）。"""
    monkeypatch.setattr(collector, "IS_LINUX", True)
    proc = tmp_path / "proc"
    (proc / "100").mkdir(parents=True)
    (proc / "100" / "maps").write_text("55d0c0a00000-55d0c0c00000 rw-p 00000000 00:00 0  [heap]\n", encoding="utf-8")
    monkeypatch.setattr(procfs, "PROC", proc)
    monkeypatch.setattr(procfs, "fd_summary", lambda pid, watched: {"total": 3})
    monkeypatch.setattr(procfs, "cgroup_memory", lambda pid: {"version": None})
    sampler = Collector(make_targets(tmp_path, worker_pid=100))
    monkeypatch.setattr(sampler, "check_worker", lambda: None)
    return sampler, proc / "100", monkeypatch


def test_trend_without_smaps_rollup_on_old_kernels(linux_trend):
    # 4.14 之前的内核没有 smaps_rollup，趋势记录照常生成，只是这一项为空。
    sampler, proc_dir, _ = linux_trend
    record = sampler.trend()
    assert record["smaps"] is None
    assert record["heap"]["main_heap_kb"] == 2048
    (proc_dir / "smaps_rollup").write_text("00-00 ---p 0 00:00 0 [rollup]\nRss:  12 kB\n", encoding="utf-8")
    assert sampler.trend()["smaps"] == {"Rss": 12}


def test_failed_trend_keeps_the_window_for_the_next_record(linux_trend):
    sampler, _, monkeypatch = linux_trend
    sampler.commit_fast(fast_sample(100, 150, 200))
    sampler.commit_fast(fast_sample(300, 350, 400))

    def unreadable(pid):
        raise FileNotFoundError(2, "No such file or directory", "memory.stat")

    monkeypatch.setattr(procfs, "cgroup_memory", unreadable)
    with pytest.raises(FileNotFoundError):
        sampler.trend()
    monkeypatch.setattr(procfs, "cgroup_memory", lambda pid: {"version": None})
    sampler.commit_fast(fast_sample(200, 250, 400))
    window = sampler.trend()["window"]
    assert (window["samples"], window["primary_min"], window["primary_max"]) == (3, 100, 300)
    # 窗口由调用方在记录写盘后清空。
    assert sampler.window.count == 3
    sampler.reset_window()
    assert sampler.window.count == 0


def test_commit_fast_feeds_the_trend_window(linux_fast):
    sampler, snapshots, monkeypatch = linux_fast
    monkeypatch.setattr(procfs, "descendants", lambda pid: ({}, 0))
    sample = sampler.fast()
    assert sampler.window.count == 0
    sample["rpc_ms"] = 7.0
    sampler.commit_fast(sample)
    assert sampler.window.count == 1 and sampler.window.rpc_ms_max == 7.0


# ---- 真实进程 ------------------------------------------------------------------


@SUPPORTED
def test_check_worker_raises_when_identity_changes(tmp_path, monkeypatch):
    as_runner_child(monkeypatch)
    targets = make_targets(tmp_path, worker_identity=identity_of(os.getpid()))
    Collector(targets).check_worker()
    targets.worker_identity = (os.getpid(), -1)
    with pytest.raises(WorkerGone):
        Collector(targets).check_worker()


@SUPPORTED
def test_check_worker_converts_unreadable_worker_into_worker_gone(tmp_path, monkeypatch):
    as_runner_child(monkeypatch)

    def unreadable(pid):
        raise OSError(31, "A device attached to the system is not functioning")

    monkeypatch.setattr(collector, "identity_of", unreadable)
    with pytest.raises(WorkerGone):
        Collector(make_targets(tmp_path)).check_worker()


@SUPPORTED
def test_check_worker_detects_reparenting_on_linux(tmp_path, monkeypatch):
    if not collector.IS_LINUX:
        pytest.skip("只有 Linux 按父进程号判断 Worker 退出")
    monkeypatch.setattr(collector.os, "getppid", lambda: 1)
    with pytest.raises(WorkerGone):
        Collector(make_targets(tmp_path, worker_identity=identity_of(os.getpid()))).check_worker()


@SUPPORTED
def test_identity_is_pid_and_start_time_without_paths():
    identity = identity_of(os.getpid())
    assert len(identity) == 2 and identity[0] == os.getpid()
    assert isinstance(identity[1], int) and identity[1] > 0


@SUPPORTED
def test_fast_and_trend_on_current_process_produce_consistent_records(tmp_path, monkeypatch):
    as_runner_child(monkeypatch)
    targets = make_targets(tmp_path, worker_identity=identity_of(os.getpid()),
                           watched_files={"review_log": tmp_path / "review_logs.json"},
                           watched_dirs={"logs": (tmp_path, "*.jsonl")})
    sampler = Collector(targets)
    first = sampler.fast()
    first["rpc_ms"] = 1.5
    sampler.commit_fast(first)
    second = sampler.fast()
    second["rpc_ms"] = None
    sampler.commit_fast(second)
    assert first["k"] == "fast" and first["worker"]["primary"] > 0 and first["self"]["rss"] > 0
    trend = sampler.trend()
    assert trend["wid"] == list(targets.worker_identity)
    assert trend["window"]["samples"] == 2
    assert trend["window"]["primary_last"] == second["worker"]["primary"]
    assert trend["window"]["rpc_ms_max"] == 1.5
    assert trend["files"] == {"review_log": None}
    assert trend["dirs"] == {"logs": {"files": 0, "bytes": 0}}
    meta = sampler.meta({"x": 1})
    assert meta["sqlite"] == sqlite3.sqlite_version and meta["x"] == 1
    linux_keys = {"smaps", "heap", "fds", "cgroup"}
    if collector.IS_LINUX:
        assert linux_keys <= set(trend)
        assert trend["smaps"]["Rss"] > 0 and trend["fds"]["total"] > 0
        assert trend["cgroup"]["version"] in (1, 2, None)
    else:
        assert not linux_keys & set(trend)
        assert first["worker"]["handles"] > 0


@LINUX_ONLY
def test_meta_records_only_whitelisted_worker_environment(tmp_path, monkeypatch):
    fake = tmp_path / "proc" / "100"
    fake.mkdir(parents=True)
    (fake / "environ").write_bytes(b"MALLOC_ARENA_MAX=2\0MAIBOT_SESSION_TOKEN=secret-token\0API_KEY=sk-secret\0")
    monkeypatch.setattr(procfs, "PROC", tmp_path / "proc")
    monkeypatch.setattr(procfs, "cgroup_memory", lambda pid: {"version": None})
    record = Collector(make_targets(tmp_path, worker_pid=100, worker_identity=(100, 5))).meta({"x": 1})
    assert record["worker_env"] == {"MALLOC_ARENA_MAX": "2"}
    assert "secret" not in json.dumps(record)
    assert "instance_root" not in record
    assert record["kernel"] and record["x"] == 1


@WINDOWS_ONLY
def test_fast_windows_aggregates_children_by_exe(tmp_path, monkeypatch):
    memory = {
        100: {"working_set": 1000, "private_usage": 800, "peak_working_set": 2000, "handle_count": 5},
        11: {"working_set": 30, "private_usage": 20},
        12: {"working_set": 10, "private_usage": 5},
        os.getpid(): {"working_set": 50, "private_usage": 40},
    }

    def process_memory(pid):
        if pid == 13:
            raise OSError(5, "Access is denied")
        return memory[pid]

    monkeypatch.setattr(collector.winproc, "process_memory", process_memory)
    monkeypatch.setattr(collector.winproc, "descendants", lambda pid: {11: "Node.exe", 12: "node.exe", 13: "x.exe"})
    sampler = Collector(make_targets(tmp_path, worker_pid=100))
    monkeypatch.setattr(sampler, "check_worker", lambda: None)
    sample = sampler.fast()
    assert sample["worker"] == {"rss": 1000, "primary": 800, "hwm": 2000, "handles": 5}
    assert sample["children"] == {"node.exe": {"n": 2, "rss": 40, "anon": 25}}
