import json
import os
import re
import threading
import time
import zipfile
from pathlib import Path

import pytest

from memdiag import dbstats, report
from memdiag.report import build_pack, format_summary, growth, human_bytes

MIB = 1024 * 1024
GIB = 1024 * MIB


def summary(**overrides):
    values = dict(latest=None, history=[], last_trend=None, db_light=None, db_heavy=None, primary_label="匿名内存",
                  worker_pid=1, collector_state="运行中", sampled_seconds=0.0, fast_interval_sec=15,
                  output_dir="data/plugins/shinoyuki.memdiag", webui_enabled=True, now=10_000.0)
    values.update(overrides)
    return format_summary(**values)


def latest_at(t, **extra):
    sample = {"t": t, "worker": {"rss": GIB, "primary": GIB, "hwm": GIB}, "self": {"rss": 60 * MIB, "anon": 40 * MIB}}
    sample.update(extra)
    return sample


def test_human_bytes_picks_unit_and_keeps_sign():
    assert human_bytes(512) == "512 B"
    assert human_bytes(3 * MIB) == "3.00 MiB"
    assert human_bytes(-1.5 * GIB) == "-1.50 GiB"
    assert human_bytes(5000 * GIB) == "5000.00 GiB"
    assert human_bytes(None) == "未知"


def test_growth_uses_last_point_not_later_than_cutoff_and_reports_span():
    now = 100_000.0
    history = [(now - 7200, 1000), (now - 3700, 1500), (now - 1800, 1700), (now, 2600)]
    assert growth(history, now, 1) == (1100, 3700)
    assert growth(history, now, 2) == (1600, 7200)
    assert growth(history, now, 6) is None
    assert growth([], now, 1) is None


def test_format_summary_reports_growth_stalled_webui_and_cgroup():
    now = 200_000.0
    latest = latest_at(
        now,
        worker={"rss": 2 * GIB, "primary": int(1.5 * GIB), "hwm": int(2.5 * GIB)},
        children={"plugin_runner": {"n": 2, "rss": 300 * MIB, "anon": 200 * MIB},
                  "node": {"n": 1, "rss": 10 * MIB, "anon": 5 * MIB, "exiting": 2}},
        webui={"listening": True, "conns": 2, "stalled": 1, "tx_max": 4 * MIB, "tx_sum": 4 * MIB},
    )
    history = [(now - 2 * 3600, GIB), (now - 3600, int(1.25 * GIB)), (now, int(1.5 * GIB))]
    trend = {
        "cgroup": {"version": 2, "current": 3 * GIB, "max": 4 * GIB, "events": {"oom_kill": 0}},
        "files": {"review_log": {"size": 12 * MIB, "mtime": 1.0}, "memory_jsonl": None},
    }
    db_heavy = {"mai_messages_total": 123456, "msg_30d_approx": {"aggregated": 9000, "tail": 12, "total": 9012}}
    text = summary(latest=latest, history=history, last_trend=trend, db_heavy=db_heavy, worker_pid=4242,
                   sampled_seconds=2 * 3600, now=now)
    assert "Worker pid=4242，采集运行中，本次已采样 2.0 小时" in text
    assert "匿名内存 1.50 GiB" in text
    assert "近 1 小时 +256.00 MiB，" in text
    assert "近 24 小时 历史不足" in text
    assert "plugin_runner ×2 300.00 MiB" in text
    assert "node ×1 10.00 MiB（另有 2 个正在退出）" in text
    assert "疑似停止读取 1 条（最大发送队列 4.00 MiB" in text
    assert "cgroup v2：用量 3.00 GiB / 4.00 GiB，OOM 杀进程 0 次" in text
    assert "review_log 12.00 MiB" in text
    assert "memory_jsonl" not in text
    assert "本插件所在 Runner RSS 60.00 MiB" in text
    assert "数据库：消息总数 123456，近 30 天约 9012 条（近似值）" in text
    assert "样本采于" not in text
    assert "样本目录：data/plugins/shinoyuki.memdiag" in text


def test_growth_span_ends_at_the_last_sample_not_at_now():
    # 采集停了 2 小时：跨度是两个样本之间的时间，不算上停掉之后到现在的空白。
    now = 100_000.0
    history = [(now - 6 * 3600, 1), (now - 4 * 3600, 5), (now - 2 * 3600, 9)]
    assert growth(history, now, 3) == (4, 2 * 3600)


@pytest.mark.parametrize(("span_hours", "annotated"), [(1.5, True), (1.2, False)])
def test_format_summary_annotates_span_only_beyond_tolerance(span_hours, annotated):
    now = 100_000.0
    history = [(now - span_hours * 3600, GIB), (now, GIB + MIB)]
    text = summary(latest=latest_at(now), history=history, now=now)
    assert ("实际跨度" in text) is annotated
    assert ("近 1 小时 +1.00 MiB（实际跨度 1.5 小时）" if annotated else "近 1 小时 +1.00 MiB，") in text


def test_format_summary_shows_actual_span_after_a_gap():
    # 插件停用 10 小时后重新启用：近 1 小时的起点其实在 10.5 小时前，必须写出来，不能当成 1 小时的增长。
    now = 100_000.0
    history = [(now - 10.5 * 3600, GIB), (now - 0.4 * 3600, GIB + 100 * MIB), (now, GIB + 200 * MIB)]
    text = summary(latest=latest_at(now), history=history, now=now)
    assert "近 1 小时 +200.00 MiB（实际跨度 10.5 小时）" in text


def test_format_summary_marks_stale_samples():
    now = 100_000.0
    text = summary(latest=latest_at(now - 3 * 3600), now=now)
    assert "（样本采于 180 分钟前）" in text
    # 快速间隔 15 秒：超过 3 个间隔（45 秒）才算过期。
    assert "（样本采于 1 分钟前）" in summary(latest=latest_at(now - 60), now=now)
    assert "样本采于" not in summary(latest=latest_at(now - 30), now=now)


def test_format_summary_reports_cgroup_without_limit_or_unavailable():
    unlimited = {"cgroup": {"version": 1, "current": GIB, "max": None, "events": {}}}
    assert "用量 1.00 GiB / 未设上限，OOM 杀进程 未知 次" in summary(latest=latest_at(10_000.0), last_trend=unlimited)
    missing = {"cgroup": {"version": 2, "path": "/", "unavailable": "未启用 memory 控制器或位于根 cgroup"}}
    assert "cgroup v2：读不到内存用量（未启用 memory 控制器或位于根 cgroup）" in summary(
        latest=latest_at(10_000.0), last_trend=missing
    )


def test_format_summary_reports_webui_state():
    latest = latest_at(10_000.0, webui={"listening": False, "conns": 0, "stalled": 0, "tx_max": 0, "tx_sum": 0})
    assert "WebUI：端口未在监听" in summary(latest=latest)
    disabled = summary(latest=latest_at(10_000.0), db_light={"mai_messages_max_id": 77}, webui_enabled=False)
    assert "WebUI：未启用" in disabled
    assert "数据库：消息最大 id 77（总数统计尚未执行）" in disabled


def test_format_summary_without_samples_and_without_directory():
    text = summary(collector_state="已停止（测试）")
    assert "采集已停止（测试）" in text
    assert "还没有采到样本。" in text
    assert "样本目录" not in summary(output_dir=None)
    assert "样本目录" not in summary(latest=latest_at(10_000.0), output_dir=None)


@pytest.fixture
def go():
    return threading.Event()


def test_build_pack_contains_meta_and_files_and_prunes_old_packs(tmp_path, go):
    sample = tmp_path / "trend.jsonl"
    sample.write_bytes(b'{"k":"trend"}\n')
    out = tmp_path / "packs"
    paths = []
    for index in range(3):
        path, stale = build_pack(out, {"samples": [sample]}, {"plugin": "memdiag"}, keep=2, cancel=go)
        assert stale == []
        paths.append(path)
        os.utime(paths[-1], (1_000_000 + index, 1_000_000 + index))
    # 只剩最新两个包，也没有残留的 .partial。
    assert sorted(p.name for p in out.iterdir()) == sorted([paths[1].name, paths[2].name])
    assert re.fullmatch(r"memdiag-\d{8}-\d{6}Z(-\d+)?\.zip", paths[2].name)
    with zipfile.ZipFile(paths[2]) as archive:
        assert sorted(archive.namelist()) == ["meta.json", "samples/trend.jsonl"]
        assert json.loads(archive.read("meta.json")) == {"plugin": "memdiag"}
        assert archive.read("samples/trend.jsonl") == b'{"k":"trend"}\n'


def test_build_pack_never_prunes_the_pack_it_just_wrote(tmp_path, go):
    # 旧包按另一个时区命名、文件名排在新包之后；按名字排序会把刚写出的包当成最旧的删掉。
    out = tmp_path / "packs"
    out.mkdir()
    for index in range(3):
        old = out / f"memdiag-29991231-23590{index}Z.zip"
        old.write_bytes(b"old")
        os.utime(old, (1_000_000 + index, 1_000_000 + index))
    path, _ = build_pack(out, {}, {}, keep=3, cancel=go)
    assert path.exists()
    assert sorted(p.name for p in out.iterdir()) == sorted([path.name, "memdiag-29991231-235901Z.zip", "memdiag-29991231-235902Z.zip"])


def test_pack_name_uses_utc(tmp_path, monkeypatch):
    utc = time.struct_time((2001, 2, 3, 4, 5, 6, 5, 34, 0))
    local = time.struct_time((2001, 2, 3, 12, 5, 6, 5, 34, 0))
    monkeypatch.setattr(report.time, "gmtime", lambda *args: utc)
    monkeypatch.setattr(report.time, "localtime", lambda *args: local)
    assert report._pack_name(tmp_path) == tmp_path / "memdiag-20010203-040506Z.zip"
    (tmp_path / "memdiag-20010203-040506Z.zip").write_bytes(b"x")
    assert report._pack_name(tmp_path) == tmp_path / "memdiag-20010203-040506Z-1.zip"


def test_build_pack_keeps_the_new_pack_when_an_old_one_cannot_be_deleted(tmp_path, go, monkeypatch):
    # Windows 上旧包正被解压软件打开时删不掉；新包已经写好，这次打包不能算失败。
    out = tmp_path / "packs"
    out.mkdir()
    old = out / "memdiag-20200101-000000Z.zip"
    old.write_bytes(b"old")
    real_unlink = Path.unlink

    def unlink(self, *args, **kwargs):
        if self == old:
            raise PermissionError(13, "being used by another process")
        real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    path, stale = build_pack(out, {}, {"k": 1}, keep=1, cancel=go)
    assert [(item, type(exc)) for item, exc in stale] == [(old, PermissionError)]
    assert sorted(p.name for p in out.iterdir()) == sorted([old.name, path.name])
    with zipfile.ZipFile(path) as archive:
        assert json.loads(archive.read("meta.json")) == {"k": 1}


def test_build_pack_removes_partial_on_failure_and_cleans_stale_leftovers(tmp_path, go):
    out = tmp_path / "packs"
    out.mkdir()
    stale = out / "memdiag-20200101-000000Z.zip.partial"
    stale.write_bytes(b"x")
    os.utime(stale, (time.time() - 2 * report.PARTIAL_MAX_AGE_SEC,) * 2)
    fresh = out / "memdiag-20200101-000001Z.zip.partial"
    fresh.write_bytes(b"x")
    with pytest.raises(FileNotFoundError):
        build_pack(out, {"samples": [tmp_path / "missing.jsonl"]}, {}, keep=1, cancel=go)
    assert sorted(p.name for p in out.iterdir()) == [fresh.name]


def test_build_pack_stops_between_files_when_cancelled(tmp_path):
    sample = tmp_path / "fast.jsonl"
    sample.write_bytes(b"{}\n")
    stop = threading.Event()
    stop.set()
    with pytest.raises(dbstats.Cancelled):
        build_pack(tmp_path / "packs", {"samples": [sample]}, {}, keep=1, cancel=stop)
    assert list((tmp_path / "packs").iterdir()) == []


def test_build_pack_rejects_keep_below_one(tmp_path, go):
    with pytest.raises(ValueError):
        build_pack(Path(tmp_path), {}, {}, keep=0, cancel=go)
