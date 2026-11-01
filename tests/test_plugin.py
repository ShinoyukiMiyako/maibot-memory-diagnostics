"""插件入口测试：按宿主的方式装载 plugin.py，注入假的宿主上下文，对测试进程本身跑真实采集。"""

from pathlib import Path
from types import SimpleNamespace

import asyncio
import importlib.util
import json
import os
import re
import sqlite3
import sys
import threading
import time
import zipfile

import pytest

pytest.importorskip("maibot_sdk")
# speed_up 把整数字段改成小数秒，model_dump 会提示类型不符；这是测试有意为之。
pytestmark = pytest.mark.filterwarnings("ignore:Pydantic serializer warnings:UserWarning")

PLUGIN_DIR = Path(__file__).resolve().parents[1]
MODULE_NAME = "_maibot_plugin_shinoyuki_memdiag"
SUPPORTED = sys.platform in ("win32", "darwin") or sys.platform.startswith("linux")
needs_platform = pytest.mark.skipif(not SUPPORTED, reason="需要 Linux、Windows 或 macOS")


def load_plugin_module():
    # 与宿主 plugin_loader 相同：plugin.py 作为包入口，插件目录作为子模块搜索路径。
    if MODULE_NAME in sys.modules:
        return sys.modules[MODULE_NAME]
    spec = importlib.util.spec_from_file_location(MODULE_NAME, PLUGIN_DIR / "plugin.py",
                                                  submodule_search_locations=[str(PLUGIN_DIR)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


plugin_module = load_plugin_module()
plugin_collector = sys.modules[f"{MODULE_NAME}.collector"]
DEFAULT_HOST_CONFIG = {"webui.enabled": False, "webui.port": 8001, "a_memorix.storage.data_dir": "data/a-memorix"}


class FakeSend:
    def __init__(self):
        self.sent = []
        self.result = True

    async def text(self, text, stream_id, **kwargs):
        self.sent.append({"text": text, "stream_id": stream_id, **kwargs})
        return self.result


class FakeConfig:
    """宿主的 config.get 能力：键缺失时返回 None。delay 模拟 Worker 主循环卡顿，fail 模拟 RPC 失败。"""

    def __init__(self, values):
        self.values = values
        self.delay = 0.0
        self.fail = False
        self.timeouts = {}

    async def call_capability(self, capability, timeout_ms=None, **kwargs):
        assert capability == "config.get"
        self.timeouts.setdefault(kwargs["key"], []).append(timeout_ms)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise ConnectionResetError("Connection lost")
        return self.values.get(kwargs["key"])


class RecordingLogger:
    def __init__(self):
        self.records = []

    def _log(self, level, message, *args):
        self.records.append((level, message % args if args else message))

    def info(self, message, *args):
        self._log("info", message, *args)

    def warning(self, message, *args):
        self._log("warning", message, *args)

    def error(self, message, *args):
        self._log("error", message, *args)

    def levels(self, level):
        return [text for name, text in self.records if name == level]


def make_instance(tmp_path):
    root = tmp_path / "instance"
    data_dir = root / "data" / "plugins" / "shinoyuki.memdiag"
    data_dir.mkdir(parents=True)
    connection = sqlite3.connect(root / "data" / "MaiBot.db")
    connection.executescript(
        "CREATE TABLE mai_messages (id INTEGER PRIMARY KEY);"
        "CREATE TABLE statistics_message_hourly (id INTEGER PRIMARY KEY, bucket_time DATETIME, message_count INTEGER);"
        "CREATE TABLE statistics_aggregation_cursors (source_name TEXT PRIMARY KEY, last_processed_id INTEGER);"
    )
    connection.executemany("INSERT INTO mai_messages (id) VALUES (?)", [(i,) for i in range(1, 6)])
    connection.commit()
    connection.close()
    memory_dir = root / "logs" / "memory_diagnostics"
    memory_dir.mkdir(parents=True)
    (memory_dir / "memory.jsonl").write_bytes(b'{"rss": 1}\n')
    return root, data_dir


def make_plugin(data_dir, config_values=None, **config_overrides):
    plugin = plugin_module.create_plugin()
    send = FakeSend()
    config_capability = FakeConfig(dict(config_values or DEFAULT_HOST_CONFIG))
    ctx = SimpleNamespace(
        send=send,
        config=config_capability,
        call_capability=config_capability.call_capability,
        paths=SimpleNamespace(data_dir=data_dir, runtime_dir=data_dir),
        logger=RecordingLogger(),
    )
    plugin._set_context(ctx)
    config = type(plugin).build_default_config()
    for section, values in config_overrides.items():
        config[section].update(values)
    plugin.set_plugin_config(config)
    return plugin, send


def speed_up(plugin, **values):
    # 配置模型校验赋值（采样间隔下限 5 秒），测试绕过校验直接改实例字段，让一轮轮采样在毫秒级发生。
    for key, value in values.items():
        object.__setattr__(plugin.cfg.collector, key, value)


@pytest.fixture
def instance(tmp_path, monkeypatch):
    """让测试进程扮演 Worker：插件 Runner 本应是它的子进程。"""
    root, data_dir = make_instance(tmp_path)
    monkeypatch.chdir(root)
    pid = os.getpid()
    monkeypatch.setattr(plugin_module, "find_worker", lambda: (pid, plugin_collector.identity_of(pid)))
    monkeypatch.setattr(plugin_collector.os, "getppid", lambda: pid)
    return root, data_dir


async def wait_until(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("等待条件超时")
        await asyncio.sleep(0.01)


def read_records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def running_collectors():
    return [task for task in asyncio.all_tasks() if task.get_name() == "memdiag.collector" and not task.done()]


# ---- 清单与组件 ------------------------------------------------------------------


def test_manifest_is_valid_utf8_without_bom_and_matches_code():
    raw = (PLUGIN_DIR / "_manifest.json").read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    manifest = json.loads(raw.decode("utf-8"))
    assert manifest["manifest_version"] == 2
    assert re.fullmatch(r"[A-Za-z0-9_]+(?:[.-][A-Za-z0-9_]+)+", manifest["id"])
    assert manifest["version"] == plugin_module.PLUGIN_VERSION
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"])
    assert manifest["capabilities"] == ["send.text", "config.get"]
    assert manifest["host_application"] == {"min_version": "1.2.3", "max_version": "1.3.99"}
    assert manifest["sdk"] == {"min_version": "2.8.0", "max_version": "2.99.99"}


def test_plugin_contract_and_single_command():
    plugin = plugin_module.create_plugin()
    base = plugin_module.MaiBotPlugin
    for name in ("on_load", "on_unload", "on_config_update"):
        assert getattr(type(plugin), name) is not getattr(base, name)
        assert asyncio.iscoroutinefunction(getattr(type(plugin), name))
    components = plugin.get_components()
    assert [component["name"] for component in components] == ["memdiag"]
    metadata = components[0]["metadata"]
    assert metadata["permission"] == "operator"
    assert metadata["intercept_message_level"] == 1
    assert metadata["aliases"] == []
    pattern = re.compile(metadata["command_pattern"])
    assert pattern.search("/memdiag").groupdict() == {"sub": None}
    assert pattern.search("  /memdiag pack ").groupdict() == {"sub": "pack"}
    for text in ("/memdiagx", "/memdiag packs", "说 /memdiag", "/memdiag other"):
        assert pattern.search(text) is None


def test_default_config_enables_plugin_and_has_version():
    config = type(plugin_module.create_plugin()).build_default_config()
    assert config["plugin"] == {"enabled": True, "config_version": "1.0.0"}
    assert config["command"]["allowed_users"] == []


def test_config_snapshot_keeps_only_the_whitelist_size(tmp_path):
    plugin, _ = make_plugin(tmp_path, command={"allowed_users": ["qq:123456789", "qq:2"]})
    snapshot = plugin_module.config_snapshot(plugin.cfg)
    assert snapshot["command"] == {"allowed_users_count": 2}
    assert "123456789" not in json.dumps(snapshot)
    assert snapshot["collector"]["fast_interval_sec"] == 15


# ---- 纯函数 ----------------------------------------------------------------------


def test_path_helpers(tmp_path, monkeypatch):
    data_dir = tmp_path / "data" / "plugins" / "shinoyuki.memdiag"
    assert plugin_module.instance_root_from(data_dir) == tmp_path
    with pytest.raises(RuntimeError):
        plugin_module.instance_root_from(tmp_path / "elsewhere" / "shinoyuki.memdiag")
    assert plugin_module.amemorix_metadata_db(tmp_path, "data/a-memorix") == (tmp_path / "data" / "a-memorix").resolve() / "metadata" / "metadata.db"
    assert plugin_module.amemorix_metadata_db(tmp_path, "  ") == (tmp_path / "data" / "a-memorix").resolve() / "metadata" / "metadata.db"
    absolute = tmp_path / "mem"
    assert plugin_module.amemorix_metadata_db(tmp_path, str(absolute)) == absolute.resolve() / "metadata" / "metadata.db"
    monkeypatch.delenv("MAIBOT_STATISTICS_REPORT_PATH", raising=False)
    assert plugin_module.statistics_report_path(tmp_path) == tmp_path / "maibot_statistics.html"
    monkeypatch.setenv("MAIBOT_STATISTICS_REPORT_PATH", str(tmp_path / "x" / "report.html"))
    assert plugin_module.statistics_report_path(tmp_path) == tmp_path / "x" / "report.html"
    with pytest.raises(FileNotFoundError):
        plugin_module.locate_main_db(tmp_path / "no-instance")


def test_load_history_filters_by_worker_and_time_and_skips_torn_lines(tmp_path):
    path = tmp_path / "trend.jsonl"
    lines = [
        {"t": 100, "k": "meta", "wid": [1, 2]},
        {"t": 200, "k": "trend", "wid": [9, 9], "window": {"primary_last": 3 << 30}},
        {"t": 50, "k": "trend", "wid": [1, 2], "window": {"primary_last": 10}},
        {"t": 300, "k": "trend", "wid": [1, 2], "window": {"primary_last": None}},
        {"t": 400, "k": "trend", "wid": [1, 2], "window": {"primary_last": 20}},
        {"t": 450, "k": "stop", "reason": "x"},
    ]
    text = "\n".join(json.dumps(line) for line in lines[:4]) + '\n{"t":350,"k":"trend","wi\n' + "\n".join(
        json.dumps(line) for line in lines[4:]
    ) + "\n"
    path.write_text(text, encoding="utf-8")
    assert plugin_module.load_history([path], [1, 2], 60) == ([(400, 20)], 1)
    assert plugin_module.load_history([], [1, 2], 0) == ([], 0)


def test_load_history_reads_the_previous_file_only_when_the_newest_starts_after_the_cutoff(tmp_path):
    old, new = tmp_path / "trend.1.jsonl", tmp_path / "trend.jsonl"

    def trend(t, value):
        return json.dumps({"t": t, "k": "trend", "wid": [1, 2], "window": {"primary_last": value}})

    old.write_text("\n".join([trend(100, 1), trend(300, 3)]) + "\n", encoding="utf-8")
    new.write_text("\n".join([json.dumps({"t": 400, "k": "meta"}), trend(500, 5)]) + "\n", encoding="utf-8")
    assert plugin_module.load_history([old, new], [1, 2], 200) == ([(300, 3), (500, 5)], 0)
    # 最新文件已经覆盖到截止点，不再读更早的文件：传一个不存在的路径，打开它就会报错。
    assert plugin_module.load_history([tmp_path / "missing.jsonl", new], [1, 2], 450) == ([(500, 5)], 0)


def test_remember_trims_by_time_and_restarts_after_clock_rollback(tmp_path):
    plugin, _ = make_plugin(tmp_path)
    # 保留 26 小时：摘要的“近 24 小时”要有一个不晚于 24 小时前的起点。
    plugin._remember(1000.0, 1)
    plugin._remember(1000.0 + 25 * 3600, 2)
    assert [value for _, value in plugin._history] == [1, 2]
    plugin._remember(1000.0 + 27 * 3600, 3)
    assert [value for _, value in plugin._history] == [2, 3]
    plugin._remember(500.0, 4)
    assert list(plugin._history) == [(500.0, 4)]
    assert any("时钟回拨" in text for text in plugin.ctx.logger.levels("warning"))


# ---- 命令 ------------------------------------------------------------------------


def test_whitelist_normalizes_platform_and_rejects_malformed_entries(tmp_path):
    plugin, _ = make_plugin(tmp_path, command={"allowed_users": ["QQ:10001", "qq: 10003"]})
    assert plugin._whitelisted("qq", " 10001 ", False)
    assert plugin._whitelisted("QQ", "10001", False)
    assert plugin._whitelisted("Qq", "10003", False)
    assert not plugin._whitelisted("qq", "10002", False)
    assert plugin._whitelisted("bot_console", "anyone", True)
    open_plugin, _ = make_plugin(tmp_path)
    assert open_plugin._whitelisted("qq", "10002", False)
    bad, _ = make_plugin(tmp_path, command={"allowed_users": ["10001"]})
    with pytest.raises(ValueError):
        bad._whitelisted("qq", "10001", False)


def test_whitelist_user_ids_are_case_sensitive(tmp_path):
    # 部分平台的用户 ID 含大写（openid）：两侧都不能转小写，否则大写用户被拒，或只差大小写的另一个用户被放行。
    plugin, _ = make_plugin(tmp_path, command={"allowed_users": ["QQ:AbC"]})
    assert plugin._whitelisted("qq", "AbC", False)
    assert not plugin._whitelisted("qq", "abc", False)


def test_denied_user_gets_reply_and_command_is_intercepted(tmp_path):
    plugin, send = make_plugin(tmp_path, command={"allowed_users": ["qq:1"]})
    result = asyncio.run(plugin.handle_memdiag(stream_id="s", matched_groups={"sub": None}, platform="qq",
                                               user_id="2", is_local_operator=False, text="/memdiag"))
    assert result == (True, "memdiag denied", True)
    assert "不在内存诊断插件的白名单" in send.sent[0]["text"]
    assert send.sent[0]["storage_message"] is False


def test_failed_send_is_an_error(tmp_path):
    plugin, send = make_plugin(tmp_path)
    send.result = False
    with pytest.raises(RuntimeError):
        asyncio.run(plugin.handle_memdiag(stream_id="s", matched_groups={"sub": None}, platform="qq", user_id="1",
                                          is_local_operator=False))


def test_pack_refused_when_collector_not_running(tmp_path):
    plugin, send = make_plugin(tmp_path)
    asyncio.run(plugin.handle_memdiag(stream_id="s", matched_groups={"sub": "pack"}, platform="qq",
                                      user_id="1", is_local_operator=False))
    assert send.sent[0]["text"] == "内存诊断插件未在运行或正在停止，无法生成诊断包"


@pytest.mark.parametrize(("key", "value", "expected"), [
    ("webui.enabled", None, bool),
    ("webui.port", True, int),
    ("webui.port", "8001", int),
])
def test_config_value_rejects_missing_and_mistyped_host_values(tmp_path, key, value, expected):
    plugin, _ = make_plugin(tmp_path, config_values={**DEFAULT_HOST_CONFIG, key: value})
    with pytest.raises(RuntimeError):
        asyncio.run(plugin._config_value(key, expected))


@needs_platform
def test_resolve_targets_reads_webui_port_only_when_enabled(instance):
    root, data_dir = instance
    enabled, _ = make_plugin(data_dir, config_values={**DEFAULT_HOST_CONFIG, "webui.enabled": True, "webui.port": 18001})
    disabled, _ = make_plugin(data_dir)

    async def scenario():
        return await enabled._resolve_targets(), await disabled._resolve_targets()

    with_webui, without_webui = asyncio.run(scenario())
    assert with_webui.webui_port == 18001
    assert without_webui.webui_port is None
    assert with_webui.db_files["main_db"] == root / "data" / "MaiBot.db"


# ---- 生命周期 --------------------------------------------------------------------


@needs_platform
def test_config_update_for_another_plugin_does_not_restart(instance):
    _, data_dir = instance
    plugin, _ = make_plugin(data_dir)

    async def scenario():
        await plugin.on_load()
        before = plugin._task
        await plugin.on_config_update("other.plugin", {}, "")
        same = plugin._task is before
        await plugin.on_config_update("self", {}, "")
        restarted = plugin._task is not before and plugin._task is not None
        await plugin.on_unload()
        return same, restarted

    assert asyncio.run(scenario()) == (True, True)


@needs_platform
def test_config_update_starts_from_a_clean_summary_state(instance):
    # 新配置下的第一轮样本出来之前，摘要不能拿旧采集的样本冒充。
    _, data_dir = instance
    plugin, _ = make_plugin(data_dir)

    async def scenario():
        await plugin.on_load()
        await wait_until(lambda: plugin._latest is not None and plugin._last_trend is not None)
        plugin._db_heavy = {"mai_messages_total": 999}
        await plugin.on_config_update("self", {}, "")
        # _ended_mono 留着旧值时摘要的采样时长会冻结甚至变成负数。
        state = (plugin._latest, plugin._last_trend, plugin._db_light, plugin._db_heavy, plugin._ended_mono, plugin._state)
        await plugin.on_unload()
        return state

    assert asyncio.run(scenario()) == (None, None, None, None, None, "运行中")


@needs_platform
def test_failed_config_update_leaves_the_summary_on_the_running_config(instance, monkeypatch):
    # 宿主先换配置再通知插件：通知失败后 self.cfg 已是新值，采集还在按旧值跑，摘要判断样本是否过旧要用旧值。
    _, data_dir = instance
    plugin, _ = make_plugin(data_dir, collector={"fast_interval_sec": 300})
    summaries = []
    original = plugin_module.report.format_summary

    def format_summary(**kwargs):
        summaries.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(plugin_module.report, "format_summary", format_summary)

    async def scenario():
        await plugin.on_load()
        running = plugin._task
        await wait_until(lambda: plugin._latest is not None)
        plugin.set_plugin_config({**plugin.cfg.model_dump(), "collector": {"fast_interval_sec": 5}})
        plugin.ctx.config.fail = True
        with pytest.raises(ConnectionResetError):
            await plugin.on_config_update("self", {}, "")
        same_collector = plugin._task is running
        failed_summary = plugin._summary_text(include_dir=False)
        plugin.ctx.config.fail = False
        await plugin.on_config_update("self", {}, "")
        recovered_summary = plugin._summary_text(include_dir=False)
        await plugin.on_unload()
        return same_collector, failed_summary, recovered_summary

    same_collector, failed_summary, recovered_summary = asyncio.run(scenario())
    assert same_collector is True
    assert summaries[0]["fast_interval_sec"] == 300
    assert "未按新配置重启：ConnectionResetError，详见日志" in failed_summary
    # 下一次配置变更成功后按新配置运行，提示随之消失。
    assert summaries[1]["fast_interval_sec"] == 5
    assert "未按新配置重启" not in recovered_summary


@needs_platform
def test_failed_config_update_keeps_the_error_text_out_of_the_reply(instance):
    # 回复可能发在群里，找不到主库的异常文本带绝对路径（常含系统用户名），只进日志。
    root, data_dir = instance
    plugin, send = make_plugin(data_dir)

    async def scenario():
        await plugin.on_load()
        # 第一轮趋势结束后，下一次读库在 300 秒之后，改名时没有连接占着主库。
        await wait_until(lambda: plugin._last_trend is not None)
        (root / "data" / "MaiBot.db").rename(root / "data" / "moved.db")
        with pytest.raises(FileNotFoundError):
            await plugin.on_config_update("self", {}, "")
        await plugin.handle_memdiag(stream_id="s", matched_groups={"sub": None}, platform="qq", user_id="1",
                                    is_local_operator=False)
        await plugin.on_unload()

    asyncio.run(scenario())
    assert "未按新配置重启：FileNotFoundError，详见日志" in send.sent[-1]["text"]
    assert "找不到主库" not in send.sent[-1]["text"]
    assert any("找不到主库" in text for text in plugin.ctx.logger.levels("error"))


@needs_platform
def test_failed_config_update_still_applies_the_whitelist(instance):
    # 白名单每次命令都读当前配置，不依赖采集重启：重启失败时，刚移除的用户也要立刻被拒。
    _, data_dir = instance
    plugin, _ = make_plugin(data_dir, command={"allowed_users": ["qq:1"]})

    async def scenario():
        await plugin.on_load()
        plugin.set_plugin_config({**plugin.cfg.model_dump(), "command": {"allowed_users": ["qq:2"]}})
        plugin.ctx.config.fail = True
        with pytest.raises(ConnectionResetError):
            await plugin.on_config_update("self", {}, "")
        allowed = plugin._whitelisted("qq", "2", False), plugin._whitelisted("qq", "1", False)
        await plugin.on_unload()
        return allowed

    assert asyncio.run(scenario()) == (True, False)


@needs_platform
def test_failed_config_update_packs_the_running_config(instance):
    _, data_dir = instance
    plugin, send = make_plugin(data_dir, collector={"sql_limit_ms": 300})

    async def scenario():
        await plugin.on_load()
        await wait_until(lambda: plugin._latest is not None)
        plugin.set_plugin_config({**plugin.cfg.model_dump(), "collector": {"sql_limit_ms": 4000}})
        plugin.ctx.config.fail = True
        with pytest.raises(ConnectionResetError):
            await plugin.on_config_update("self", {}, "")
        plugin.ctx.config.fail = False
        await plugin.handle_memdiag(stream_id="s", matched_groups={"sub": "pack"}, platform="qq", user_id="1",
                                    is_local_operator=False)
        await wait_until(lambda: len(send.sent) >= 2)
        await plugin.on_unload()

    asyncio.run(scenario())
    pack = next((data_dir / "packs").glob("memdiag-*.zip"))
    with zipfile.ZipFile(pack) as archive:
        meta = json.loads(archive.read("meta.json"))
    # 样本是按旧配置采的，包里的配置要与样本一致。
    assert meta["config"]["collector"]["sql_limit_ms"] == 300


@needs_platform
def test_unload_during_a_config_update_leaves_no_collector(instance):
    _, data_dir = instance
    plugin, _ = make_plugin(data_dir)

    async def scenario():
        await plugin.on_load()
        plugin.ctx.config.delay = 0.1
        update = asyncio.create_task(plugin.on_config_update("self", {}, ""))
        await asyncio.sleep(0.05)
        await plugin.on_unload()
        await update
        await asyncio.sleep(0.05)
        return plugin._task, running_collectors()

    assert asyncio.run(scenario()) == (None, [])


@needs_platform
def test_overlapping_config_updates_keep_a_single_collector(instance):
    _, data_dir = instance
    plugin, _ = make_plugin(data_dir)

    async def scenario():
        await plugin.on_load()
        plugin.ctx.config.delay = 0.05
        await asyncio.gather(plugin.on_config_update("self", {}, ""), plugin.on_config_update("self", {}, ""))
        during = running_collectors()
        current = plugin._task
        await plugin.on_unload()
        await asyncio.sleep(0.05)
        return during, current, running_collectors()

    during, current, after = asyncio.run(scenario())
    assert during == [current]
    assert after == []


@needs_platform
def test_unload_stops_the_collector_through_the_stop_event(instance):
    _, data_dir = instance
    plugin, _ = make_plugin(data_dir)

    async def scenario():
        await plugin.on_load()
        await wait_until(lambda: plugin._latest is not None)
        task, cancel = plugin._task, plugin._cancel
        started = time.monotonic()
        await plugin.on_unload()
        return task.done() and not task.cancelled(), cancel, time.monotonic() - started

    stopped, cancel, elapsed = asyncio.run(scenario())
    assert stopped
    assert cancel.is_set()
    assert elapsed < plugin_module.STOP_WAIT_SEC / 2
    assert plugin._state == "已停止（插件卸载或配置变更）"
    assert plugin._ended_mono is not None


@needs_platform
def test_unload_cancels_a_collector_that_ignores_the_stop_event(instance):
    _, data_dir = instance
    plugin, _ = make_plugin(data_dir)

    async def stubborn(*args):
        await asyncio.sleep(3600)

    plugin._run = stubborn

    async def scenario():
        await plugin.on_load()
        task = plugin._task
        started = time.monotonic()
        await plugin.on_unload()
        # 在事件循环还在时检查：asyncio.run 收尾会取消所有剩下的任务，放到外面断言测不出什么。
        return task.cancelled(), time.monotonic() - started

    cancelled, elapsed = asyncio.run(scenario())
    assert cancelled
    # 宿主关停时同一 Runner 的所有插件共用约 1.5 秒。
    assert elapsed < 1.2


@needs_platform
def test_worker_gone_ends_collection_with_a_stop_record(instance, monkeypatch):
    _, data_dir = instance
    plugin, _ = make_plugin(data_dir)

    def gone(self):
        raise plugin_module.WorkerGone("Worker 已退出（测试）")

    monkeypatch.setattr(plugin_module.Collector, "fast", gone)

    async def scenario():
        await plugin.on_load()
        task = plugin._task
        await wait_until(task.done)
        return task

    task = asyncio.run(scenario())
    assert not task.cancelled() and task.exception() is None
    assert plugin._state.startswith("已停止（") and "测试" in plugin._state
    assert read_records(data_dir / "trend.jsonl")[-1]["k"] == "stop"
    assert plugin.ctx.logger.levels("error") == []


@needs_platform
def test_consecutive_failures_stop_collection_after_the_limit(instance, monkeypatch):
    _, data_dir = instance
    plugin, _ = make_plugin(data_dir, collector={"max_consecutive_failures": 3})
    speed_up(plugin, fast_interval_sec=0.02)

    def broken(self):
        raise RuntimeError("读不到 status（测试）")

    monkeypatch.setattr(plugin_module.Collector, "fast", broken)

    async def scenario():
        await plugin.on_load()
        task = plugin._task
        await wait_until(lambda: "异常" in plugin._state)
        ended = plugin._ended_mono
        await asyncio.sleep(0.05)
        # 之后的卸载不能抹掉失败原因，也不改结束时间。
        await plugin.on_unload()
        return task, ended

    task, ended = asyncio.run(scenario())
    assert isinstance(task.exception(), RuntimeError)
    errors = [record for record in read_records(data_dir / "trend.jsonl") if record["k"] == "error"]
    assert [(record["kind"], record["n"]) for record in errors] == [("fast", 1), ("fast", 2), ("fast", 3)]
    assert "异常" in plugin._state
    assert ended is not None and plugin._ended_mono == ended


@needs_platform
def test_unload_right_after_a_failure_keeps_the_failure_reason(instance, monkeypatch):
    # 卸载可能正好落在任务因异常结束与它的完成回调之间：回调执行时 _task 已被清空，原因只能由停止流程记下。
    _, data_dir = instance
    plugin, _ = make_plugin(data_dir, collector={"max_consecutive_failures": 1})

    def broken(self):
        raise RuntimeError("读不到 status（测试）")

    monkeypatch.setattr(plugin_module.Collector, "fast", broken)

    async def scenario():
        await plugin.on_load()
        task = plugin._task
        while not task.done():
            await asyncio.sleep(0)
        callback_ran = "异常" in plugin._state
        await plugin.on_unload()
        return callback_ran

    assert asyncio.run(scenario()) is False
    assert plugin._state == "已停止（异常：RuntimeError('读不到 status（测试）')）"
    assert plugin._ended_mono is not None


@needs_platform
def test_failure_counter_resets_after_success_and_is_kept_per_kind(instance, monkeypatch):
    _, data_dir = instance
    plugin, _ = make_plugin(data_dir, collector={"max_consecutive_failures": 2})
    speed_up(plugin, fast_interval_sec=0.02, trend_interval_sec=0.05)
    original_fast = plugin_module.Collector.fast
    calls = {"fast": 0}

    def flaky_fast(self):
        calls["fast"] += 1
        if calls["fast"] in (1, 3):
            raise RuntimeError("偶发失败（测试）")
        return original_fast(self)

    def broken_trend(self):
        raise RuntimeError("趋势失败（测试）")

    monkeypatch.setattr(plugin_module.Collector, "fast", flaky_fast)
    monkeypatch.setattr(plugin_module.Collector, "trend", broken_trend)

    async def scenario():
        await plugin.on_load()
        task = plugin._task
        await wait_until(task.done)
        return task

    task = asyncio.run(scenario())
    assert str(task.exception()) == "趋势失败（测试）"
    errors = [(record["kind"], record["n"]) for record in read_records(data_dir / "trend.jsonl") if record["k"] == "error"]
    assert [entry for entry in errors if entry[0] == "fast"] == [("fast", 1), ("fast", 1)]
    assert [entry for entry in errors if entry[0] == "trend"] == [("trend", 1), ("trend", 2)]


@needs_platform
def test_slow_or_failing_rpc_does_not_block_or_fail_sampling(instance):
    _, data_dir = instance
    plugin, _ = make_plugin(data_dir, collector={"max_consecutive_failures": 2})
    speed_up(plugin, fast_interval_sec=0.02)

    def probes():
        return [task for task in asyncio.all_tasks() if task.get_name() == "memdiag.rpc_probe" and not task.done()]

    async def scenario():
        await plugin.on_load()
        plugin.ctx.config.delay = 1.0
        # 探测挂着的这段时间里每轮都在采样，等的是同一个请求，不会每轮再发一个。
        await wait_until(lambda: plugin._latest is not None and plugin._latest.get("rpc_pending_s", 0) >= 0.3)
        in_flight = len(probes())
        plugin.ctx.config.delay = 0.0
        plugin.ctx.config.fail = True
        await wait_until(lambda: "rpc_error" in plugin._latest)
        rounds = len(read_records(data_dir / "fast.jsonl"))
        await wait_until(lambda: len(read_records(data_dir / "fast.jsonl")) >= rounds + 5)
        still_running = not plugin._task.done()
        plugin.ctx.config.fail = False
        plugin.ctx.config.delay = 3600
        await wait_until(lambda: probes() != [])
        await plugin.on_unload()
        return still_running, in_flight, probes()

    still_running, in_flight, left = asyncio.run(scenario())
    assert still_running is True
    assert in_flight == 1
    # 卸载取消挂着的探测，它不会带着旧实例的引用一直等下去。
    assert left == []
    samples = read_records(data_dir / "fast.jsonl")
    pending = [sample["rpc_pending_s"] for sample in samples if "rpc_pending_s" in sample]
    assert all(sample["rpc_ms"] is None for sample in samples if "rpc_pending_s" in sample)
    # 挂起时长从发出请求算起：同一个请求挂着的每一轮都在增长，量级是秒而不是进程运行时长。
    assert max(pending) >= 0.3 and all(0 <= value < 5 for value in pending)
    assert any("ConnectionResetError" in sample.get("rpc_error", "") for sample in samples)
    assert not any(record["k"] == "error" for record in read_records(data_dir / "trend.jsonl"))
    assert len([text for text in plugin.ctx.logger.levels("warning") if "RPC 探测失败" in text]) == 1


@needs_platform
def test_trend_rounds_count_new_messages_since_previous_round(instance):
    root, data_dir = instance
    plugin, _ = make_plugin(data_dir)
    speed_up(plugin, fast_interval_sec=0.02, trend_interval_sec=0.05)

    async def scenario():
        await plugin.on_load()
        await wait_until(lambda: plugin._last_trend is not None)
        connection = sqlite3.connect(root / "data" / "MaiBot.db")
        connection.executemany("INSERT INTO mai_messages (id) VALUES (?)", [(6,), (7,), (8,)])
        connection.commit()
        connection.close()
        await wait_until(lambda: plugin._last_trend["db"].get("mai_messages_max_id") == 8)
        await plugin.on_unload()

    asyncio.run(scenario())
    trends = [record for record in read_records(data_dir / "trend.jsonl") if record["k"] == "trend"]
    deltas = [record["db"].get("mai_messages_delta") for record in trends]
    assert deltas[0] is None
    assert 3 in deltas
    assert plugin._history[-1][1] == trends[-1]["window"]["primary_last"]


@needs_platform
def test_second_pack_request_waits_and_failures_are_replied(instance):
    _, data_dir = instance
    plugin, send = make_plugin(data_dir)
    release = threading.Event()

    def blocked(*args):
        release.wait(5)
        raise OSError(28, "No space left on device")

    plugin._build_pack_blocking = blocked

    async def scenario():
        await plugin.on_load()
        command = dict(stream_id="s", matched_groups={"sub": "pack"}, platform="qq", user_id="1", is_local_operator=False)
        await plugin.handle_memdiag(**command)
        await plugin.handle_memdiag(**command)
        pack = plugin._pack_task
        release.set()
        with pytest.raises(OSError):
            await pack
        await plugin.on_unload()

    asyncio.run(scenario())
    texts = [message["text"] for message in send.sent]
    assert texts[:2] == ["开始生成诊断包，完成后另行回复", "已有诊断包在生成中，请稍候"]
    assert texts[2].startswith("诊断包生成失败：OSError(28")


@needs_platform
def test_unload_interrupts_a_running_pack(instance):
    _, data_dir = instance
    plugin, send = make_plugin(data_dir)
    seen = {}

    def waits_for_cancel(*args):
        cancel = args[-1]
        seen["cancel"] = cancel
        cancel.wait(5)
        raise plugin_module.dbstats.Cancelled("采集正在停止")

    plugin._build_pack_blocking = waits_for_cancel

    async def scenario():
        await plugin.on_load()
        await plugin.handle_memdiag(stream_id="s", matched_groups={"sub": "pack"}, platform="qq", user_id="1",
                                    is_local_operator=False)
        await wait_until(lambda: "cancel" in seen)
        pack = plugin._pack_task
        started = time.monotonic()
        await plugin.on_unload()
        return pack.cancelled(), time.monotonic() - started

    cancelled, elapsed = asyncio.run(scenario())
    assert seen["cancel"].is_set()
    assert cancelled
    assert elapsed < 1.0
    assert [message["text"] for message in send.sent] == ["开始生成诊断包，完成后另行回复"]


@needs_platform
def test_end_to_end_collect_summarize_pack_and_unload(instance):
    _, data_dir = instance
    plugin, send = make_plugin(data_dir, command={"allowed_users": ["qq:1"]})

    async def scenario():
        await plugin.on_load()
        await wait_until(lambda: plugin._latest is not None and plugin._last_trend is not None)
        assert plugin._state == "运行中"
        await plugin.handle_memdiag(stream_id="s", matched_groups={"sub": None}, platform="qq", user_id="1",
                                    is_local_operator=False)
        await plugin.handle_memdiag(stream_id="s", matched_groups={"sub": "pack"}, platform="qq", user_id="1",
                                    is_local_operator=False)
        await asyncio.wait_for(plugin._pack_task, timeout=30)
        await plugin.on_unload()

    asyncio.run(scenario())
    summary, started, finished = (message["text"] for message in send.sent)
    sample_dir = os.path.join("data", "plugins", "shinoyuki.memdiag")
    assert f"Worker pid={os.getpid()}，采集运行中" in summary
    assert "WebUI：未启用" in summary
    assert "数据库：消息最大 id 5" in summary
    assert f"样本目录：{sample_dir}" in summary
    assert started == "开始生成诊断包，完成后另行回复"
    assert finished.startswith(f"诊断包已生成：{os.path.join(sample_dir, 'packs', 'memdiag-')}")
    trend_lines = read_records(data_dir / "trend.jsonl")
    assert [record["k"] for record in trend_lines][:2] == ["meta", "trend"]
    assert trend_lines[0]["plugin_version"] == plugin_module.PLUGIN_VERSION
    assert trend_lines[0]["sqlite"] == sqlite3.sqlite_version
    assert trend_lines[0]["config"]["command"] == {"allowed_users_count": 1}
    assert "instance_root" not in trend_lines[0]
    assert trend_lines[1]["db"]["mai_messages_max_id"] == 5
    assert plugin._history and plugin._history[-1][1] == trend_lines[1]["window"]["primary_last"]
    packs = list((data_dir / "packs").glob("memdiag-*.zip"))
    assert len(packs) == 1
    with zipfile.ZipFile(packs[0]) as archive:
        names = set(archive.namelist())
        assert {"meta.json", "samples/fast.jsonl", "samples/trend.jsonl", "host_memory_diagnostics/memory.jsonl"} <= names
        meta = json.loads(archive.read("meta.json"))
    assert meta["db_heavy"]["mai_messages_total"] == 5
    assert meta["sqlite"] == sqlite3.sqlite_version
    assert meta["wid"] == list(plugin_collector.identity_of(os.getpid()))
    assert "Worker pid=" in meta["summary"] and "样本目录" not in meta["summary"]
    assert meta["config"]["command"] == {"allowed_users_count": 1}


# ---- RPC 探测 --------------------------------------------------------------------


def test_rpc_probe_keeps_one_request_in_flight_and_reports_its_real_duration():
    async def scenario():
        release = asyncio.Event()
        calls = []

        async def call():
            calls.append(time.monotonic())
            await release.wait()
            return 1234.5

        probe = plugin_module.RpcProbe(call)
        first = await probe.measure(0.15)
        await asyncio.sleep(0.3)
        second = await probe.measure(0.1)
        release.set()
        third = await probe.measure(1.0)
        return len(calls), first, second, third

    calls, first, second, third = asyncio.run(scenario())
    assert calls == 1
    assert first["rpc_ms"] is None and second["rpc_ms"] is None
    assert 0 < first["rpc_pending_s"] < second["rpc_pending_s"] < 1
    assert third == {"rpc_ms": 1234.5}


def test_rpc_probe_measures_milliseconds(tmp_path):
    plugin, _ = make_plugin(tmp_path)
    plugin.ctx.config.delay = 0.2
    elapsed = asyncio.run(plugin._probe_rpc_ms())
    # 下界留出一个时钟分辨率：asyncio.sleep 按 monotonic 计时，在 Windows 上粒度约 15 毫秒。
    assert 150 <= elapsed < 5000


def test_rpc_probe_asks_for_more_than_the_default_rpc_timeout(tmp_path):
    # 宿主默认 30 秒超时：超过它的卡顿会被切成几段，rpc_ms 记不下真实时长。
    plugin, _ = make_plugin(tmp_path)
    asyncio.run(plugin._probe_rpc_ms())
    assert plugin.ctx.config.timeouts["webui.enabled"] == [plugin_module.PROBE_TIMEOUT_MS]
    assert plugin_module.PROBE_TIMEOUT_MS > 30_000


@needs_platform
@pytest.mark.parametrize(("interval", "expected"), [(6, 3.0), (15, 5.0), (300, 5.0)])
def test_each_round_waits_for_the_probe_at_most_five_seconds(instance, monkeypatch, interval, expected):
    # 等得更久的话，Worker 主循环卡住时这一轮的样本迟迟不落盘，OOM 前最后的样本就丢了。
    _, data_dir = instance
    plugin, _ = make_plugin(data_dir, collector={"fast_interval_sec": interval})
    waits = []

    async def measure(self, wait_sec):
        waits.append(wait_sec)
        return {"rpc_ms": 1.0}

    monkeypatch.setattr(plugin_module.RpcProbe, "measure", measure)

    async def scenario():
        await plugin.on_load()
        await wait_until(lambda: waits != [])
        await plugin.on_unload()

    asyncio.run(scenario())
    assert waits[0] == expected


def test_rpc_probe_close_cancels_the_request():
    async def scenario():
        async def call():
            await asyncio.sleep(3600)
            return 0.0

        probe = plugin_module.RpcProbe(call)
        await probe.measure(0.01)
        task = probe._task
        probe.close()
        await asyncio.wait({task}, timeout=1)
        return task.cancelled(), probe._task

    assert asyncio.run(scenario()) == (True, None)


@needs_platform
def test_each_round_samples_before_probing(instance, monkeypatch):
    # 先读进程外的样本再发探测：Worker 主循环卡住时，这一轮的内存读数不会被探测的等待推迟。
    _, data_dir = instance
    plugin, _ = make_plugin(data_dir)
    speed_up(plugin, fast_interval_sec=0.02)
    events = []
    original_fast = plugin_module.Collector.fast

    def fast(self):
        events.append("fast")
        return original_fast(self)

    monkeypatch.setattr(plugin_module.Collector, "fast", fast)
    original_probe = plugin._probe_rpc_ms

    async def probe():
        events.append("probe")
        return await original_probe()

    plugin._probe_rpc_ms = probe

    async def scenario():
        await plugin.on_load()
        await wait_until(lambda: len(events) >= 4)
        await plugin.on_unload()

    asyncio.run(scenario())
    assert events[:4] == ["fast", "probe", "fast", "probe"]


# ---- 生命周期的边界 --------------------------------------------------------------


@needs_platform
def test_reloading_the_same_instance_after_unload_restarts_collection(instance):
    # 两版宿主重载失败回滚时，会对卸载过的同一个实例再调 on_load。
    _, data_dir = instance
    plugin, _ = make_plugin(data_dir)

    async def scenario():
        await plugin.on_load()
        await plugin.on_unload()
        await plugin.on_load()
        state = (running_collectors() == [plugin._task], plugin._state)
        await plugin.on_unload()
        return state

    assert asyncio.run(scenario()) == (True, "运行中")


@needs_platform
def test_unload_waits_for_a_collector_that_a_config_update_is_stopping(instance):
    _, data_dir = instance
    plugin, _ = make_plugin(data_dir)

    async def stubborn(*args):
        await asyncio.sleep(3600)

    plugin._run = stubborn

    async def scenario():
        await plugin.on_load()
        update = asyncio.create_task(plugin.on_config_update("self", {}, ""))
        await wait_until(lambda: plugin._stopping is not None)
        await plugin.on_unload()
        # on_unload 返回时旧任务必须已经结束：重载出的新实例马上会写同一组文件。
        alive = running_collectors()
        await update
        return alive, plugin._task, running_collectors()

    assert asyncio.run(scenario()) == ([], None, [])


@needs_platform
def test_config_update_that_cannot_resolve_targets_keeps_the_running_collector(instance):
    # 解析目标要发 RPC，Worker 主循环卡住时会失败；这时不能先把正在跑的采集停掉。
    _, data_dir = instance
    plugin, _ = make_plugin(data_dir)

    async def scenario():
        await plugin.on_load()
        await wait_until(lambda: plugin._latest is not None)
        before = plugin._task
        plugin.ctx.config.fail = True
        with pytest.raises(ConnectionResetError):
            await plugin.on_config_update("self", {}, "")
        kept = plugin._task is before and not before.done() and plugin._state == "运行中"
        plugin.ctx.config.fail = False
        await plugin.on_unload()
        return kept

    assert asyncio.run(scenario()) is True
    assert any("解析采集目标失败" in text for text in plugin.ctx.logger.levels("error"))


@needs_platform
def test_unload_during_an_in_flight_query_is_a_normal_stop(instance, monkeypatch):
    _, data_dir = instance
    plugin, _ = make_plugin(data_dir)
    entered = threading.Event()

    def blocking_light_stats(db, tables, previous, limit_ms, *, cancel):
        entered.set()
        cancel.wait(5)
        plugin_module.dbstats.raise_if_cancelled(cancel)
        raise AssertionError("停止信号没有到达查询")

    monkeypatch.setattr(plugin_module.dbstats, "light_stats", blocking_light_stats)

    async def scenario():
        await plugin.on_load()
        task = plugin._task
        await wait_until(entered.is_set)
        await plugin.on_unload()
        if not task.done() or task.cancelled():
            return "未正常结束"
        return task.exception()

    assert asyncio.run(scenario()) is None
    assert plugin.ctx.logger.levels("error") == []
    assert plugin._state == "已停止（插件卸载或配置变更）"


def test_a_superseded_collector_failing_does_not_change_the_current_state(tmp_path):
    plugin, _ = make_plugin(tmp_path)

    async def scenario():
        async def boom():
            raise RuntimeError("旧任务失败（测试）")

        async def idle():
            await asyncio.sleep(3600)

        old = asyncio.create_task(boom())
        await asyncio.wait({old})
        plugin._task = asyncio.create_task(idle())
        plugin._state = "运行中"
        plugin._on_collector_done(old)
        plugin._task.cancel()
        return plugin._state, plugin._ended_mono

    assert asyncio.run(scenario()) == ("运行中", None)
    assert any("旧任务失败（测试）" in text for text in plugin.ctx.logger.levels("error"))


@needs_platform
def test_pack_refused_after_unload(instance):
    _, data_dir = instance
    plugin, send = make_plugin(data_dir)

    async def scenario():
        await plugin.on_load()
        await plugin.on_unload()
        await plugin.handle_memdiag(stream_id="s", matched_groups={"sub": "pack"}, platform="qq", user_id="1",
                                    is_local_operator=False)
        return plugin._pack_task

    assert asyncio.run(scenario()) is None
    assert send.sent[-1]["text"] == "内存诊断插件未在运行或正在停止，无法生成诊断包"


# ---- 采集的失败处理 --------------------------------------------------------------


@needs_platform
def test_failing_to_write_an_error_marker_does_not_end_collection(instance, monkeypatch):
    # 磁盘短暂写满：采样失败，接着写 error 标记也失败；结束采集仍只看连续失败次数。
    _, data_dir = instance
    plugin, _ = make_plugin(data_dir, collector={"max_consecutive_failures": 3})
    speed_up(plugin, fast_interval_sec=0.02)
    original_fast = plugin_module.Collector.fast
    calls = {"fast": 0}

    def flaky_fast(self):
        calls["fast"] += 1
        if calls["fast"] == 1:
            raise OSError(28, "No space left on device")
        return original_fast(self)

    original_append = plugin_module.storage.RotatingJsonl.append

    def append(self, record):
        if record["k"] == "error":
            raise OSError(28, "No space left on device")
        original_append(self, record)

    monkeypatch.setattr(plugin_module.Collector, "fast", flaky_fast)
    monkeypatch.setattr(plugin_module.storage.RotatingJsonl, "append", append)

    async def scenario():
        await plugin.on_load()
        await wait_until(lambda: plugin._latest is not None)
        alive = not plugin._task.done()
        await plugin.on_unload()
        return alive

    assert asyncio.run(scenario()) is True
    errors = plugin.ctx.logger.levels("error")
    assert any("fast 采样失败（连续第 1 次）" in text for text in errors)
    assert any("写入 error 标记失败" in text for text in errors)
    assert not any("采集任务异常退出" in text for text in errors)


@needs_platform
def test_worker_gone_reason_survives_a_failed_stop_marker(instance, monkeypatch):
    _, data_dir = instance
    plugin, _ = make_plugin(data_dir)

    def gone(self):
        raise plugin_module.WorkerGone("Worker 已退出（测试）")

    original_append = plugin_module.storage.RotatingJsonl.append

    def append(self, record):
        if record["k"] == "stop":
            raise OSError(28, "No space left on device")
        original_append(self, record)

    monkeypatch.setattr(plugin_module.Collector, "fast", gone)
    monkeypatch.setattr(plugin_module.storage.RotatingJsonl, "append", append)

    async def scenario():
        await plugin.on_load()
        task = plugin._task
        await wait_until(task.done)
        return task.exception()

    assert asyncio.run(scenario()) is None
    assert plugin._state == "已停止（Worker 已退出（测试））"
    assert any("写入 stop 标记失败" in text for text in plugin.ctx.logger.levels("error"))


def test_trend_round_resets_the_window_only_after_the_record_is_written(tmp_path, monkeypatch):
    events = []

    class FakeCollector:
        targets = SimpleNamespace(db_files={"main_db": tmp_path / "MaiBot.db"})

        def trend(self):
            events.append("trend")
            return {"k": "trend"}

        def reset_window(self):
            events.append("reset")

    class FakeSink:
        def append(self, record):
            events.append("append")

    def light_stats(*args, **kwargs):
        events.append("db")
        return {}

    go = threading.Event()
    monkeypatch.setattr(plugin_module.dbstats, "light_stats", light_stats)
    plugin_module.MemDiagPlugin._trend_round(FakeCollector(), FakeSink(), [], None, 300, go)
    assert events == ["trend", "db", "append", "reset"]

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(plugin_module.dbstats, "light_stats", locked)
    events.clear()
    with pytest.raises(sqlite3.OperationalError):
        plugin_module.MemDiagPlugin._trend_round(FakeCollector(), FakeSink(), [], None, 300, go)
    # 这一轮没写盘，窗口留给下一条记录。
    assert events == ["trend"]


@needs_platform
def test_trend_rounds_keep_the_message_baseline_when_max_id_times_out(instance, monkeypatch):
    _, data_dir = instance
    plugin, _ = make_plugin(data_dir)
    speed_up(plugin, fast_interval_sec=0.02, trend_interval_sec=0.03)
    results = [{"mai_messages_max_id": 5}, {"mai_messages_max_id": {"timeout_ms": 300}}, {"mai_messages_max_id": 9}]
    seen = []

    def light_stats(db, tables, previous, limit_ms, *, cancel):
        seen.append(previous)
        return dict(results[min(len(seen), len(results)) - 1])

    monkeypatch.setattr(plugin_module.dbstats, "light_stats", light_stats)

    async def scenario():
        await plugin.on_load()
        await wait_until(lambda: len(seen) >= 4)
        await plugin.on_unload()

    asyncio.run(scenario())
    # 超时那一轮之后仍按上一个有效的最大 id 计新增消息。
    assert seen[:4] == [None, 5, 5, 9]


@needs_platform
def test_start_removes_backups_beyond_the_configured_count(instance, monkeypatch):
    _, data_dir = instance
    plugin, _ = make_plugin(data_dir, storage={"fast_backups": 2})
    for name in ("fast.3.jsonl", "fast.4.jsonl"):
        (data_dir / name).write_bytes(b"{}\n")
    real_unlink = Path.unlink

    def unlink(self, *args, **kwargs):
        if self.name == "fast.4.jsonl":
            raise PermissionError(13, "being used by another process")
        real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)

    async def scenario():
        await plugin.on_load()
        await wait_until(lambda: plugin._latest is not None)
        await plugin.on_unload()

    asyncio.run(scenario())
    assert not (data_dir / "fast.3.jsonl").exists() and (data_dir / "fast.4.jsonl").exists()
    assert any("fast.3.jsonl" in text for text in plugin.ctx.logger.levels("info"))
    assert any("fast.4.jsonl" in text and "删不掉" in text for text in plugin.ctx.logger.levels("warning"))


@needs_platform
def test_failed_start_meta_is_retried_instead_of_ending_collection(instance, monkeypatch):
    _, data_dir = instance
    plugin, _ = make_plugin(data_dir, collector={"max_consecutive_failures": 100})
    speed_up(plugin, trend_interval_sec=0.05)
    blocked = threading.Event()
    blocked.set()
    written = []
    original = plugin_module.storage.RotatingJsonl.append

    def append(self, record):
        if record["k"] == "meta" and blocked.is_set():
            raise PermissionError(13, "being used by another process")
        original(self, record)
        written.append(record["k"])

    monkeypatch.setattr(plugin_module.storage.RotatingJsonl, "append", append)

    async def scenario():
        await plugin.on_load()
        task = plugin._task
        await wait_until(lambda: "error" in written)
        still_running = not task.done()
        blocked.clear()
        await wait_until(lambda: written.count("trend") >= 2)
        await plugin.on_unload()
        return still_running

    assert asyncio.run(scenario()) is True
    errors = [record for record in read_records(data_dir / "trend.jsonl") if record["k"] == "error"]
    assert (errors[0]["kind"], errors[0]["n"]) == ("trend", 1)
    # 只写成一次，且排在第一条趋势记录之前；重试期间到点的 db 记录可能排在它前面。
    assert written.count("meta") == 1
    assert written.index("meta") < written.index("trend")


@needs_platform
def test_start_meta_that_keeps_failing_stops_at_the_failure_limit(instance, monkeypatch):
    _, data_dir = instance
    plugin, _ = make_plugin(data_dir, collector={"max_consecutive_failures": 2})
    speed_up(plugin, trend_interval_sec=0.02)
    original = plugin_module.storage.RotatingJsonl.append

    def append(self, record):
        if record["k"] == "meta":
            raise PermissionError(13, "being used by another process")
        original(self, record)

    monkeypatch.setattr(plugin_module.storage.RotatingJsonl, "append", append)

    async def scenario():
        await plugin.on_load()
        await wait_until(lambda: "异常" in plugin._state)
        await plugin.on_unload()

    asyncio.run(scenario())
    errors = [record for record in read_records(data_dir / "trend.jsonl") if record["k"] == "error"]
    assert [(record["kind"], record["n"]) for record in errors] == [("trend", 1), ("trend", 2)]


@needs_platform
def test_history_is_restored_across_rotation_and_restarts(instance):
    _, data_dir = instance
    plugin, _ = make_plugin(data_dir, storage={"max_file_mb": 1})
    wid = list(plugin_collector.identity_of(os.getpid()))
    now = time.time()

    def trend(hours_ago, value):
        return json.dumps({"t": now - hours_ago * 3600, "k": "trend", "wid": wid, "window": {"primary_last": value}})

    # 更早的历史已经轮转进 trend.1.jsonl；trend.jsonl 离上限只差 50 字节，这次启动写的 meta 会让它再轮转一次。
    (data_dir / "trend.1.jsonl").write_text("\n".join(trend(h, h) for h in (25, 20, 10)) + "\n", encoding="utf-8")
    tail = "\n".join(trend(h, h) for h in (2, 1)) + "\n"
    template = json.dumps({"t": now - 3 * 3600, "k": "pad", "x": ""}) + "\n"
    filler = 1024 * 1024 - 50 - len(template.encode()) - len(tail.encode())
    pad = json.dumps({"t": now - 3 * 3600, "k": "pad", "x": "x" * filler}) + "\n"
    (data_dir / "trend.jsonl").write_text(pad + tail, encoding="utf-8")

    async def scenario():
        await plugin.on_load()
        await wait_until(lambda: plugin._last_trend is not None)
        restored = [value for _, value in plugin._history][:5]
        await plugin.on_config_update("self", {}, "")
        await wait_until(lambda: plugin._last_trend is not None)
        again = list(plugin._history)
        await plugin.on_unload()
        return restored, again

    restored, again = asyncio.run(scenario())
    assert restored == [25, 20, 10, 2, 1]
    # 重启后从文件重新恢复，内存里的旧历史不留：否则恢复出的早期点排在新点之后，会被当成时钟回拨清空。
    assert [value for _, value in again][:5] == [25, 20, 10, 2, 1]
    times = [t for t, _ in again]
    assert times == sorted(set(times))
    assert (data_dir / "trend.2.jsonl").exists()
    assert not any("时钟回拨" in text for text in plugin.ctx.logger.levels("warning"))


@needs_platform
def test_pack_reply_mentions_old_packs_that_could_not_be_deleted(instance):
    _, data_dir = instance
    plugin, send = make_plugin(data_dir)
    made = data_dir / "packs" / "memdiag-20261029-000000Z.zip"
    old = data_dir / "packs" / "memdiag-20200101-000000Z.zip"

    def build(*args):
        made.parent.mkdir(parents=True, exist_ok=True)
        made.write_bytes(b"zip")
        return made, [(old, PermissionError(13, "being used by another process"))]

    plugin._build_pack_blocking = build

    async def scenario():
        await plugin.on_load()
        await plugin.handle_memdiag(stream_id="s", matched_groups={"sub": "pack"}, platform="qq", user_id="1",
                                    is_local_operator=False)
        await asyncio.wait_for(plugin._pack_task, timeout=10)
        await plugin.on_unload()

    asyncio.run(scenario())
    finished = send.sent[-1]["text"]
    assert finished.startswith("诊断包已生成：") and "另有 1 个旧包删不掉" in finished
    assert any(old.name in text for text in plugin.ctx.logger.levels("warning"))
