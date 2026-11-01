"""内存诊断插件入口。

插件运行在宿主拉起的插件 Runner 子进程里，碰不到 Worker 的 Python 堆，只能从进程外周期采集：
Worker 及其子进程的内存（/proc、Windows API 或 libproc）、cgroup、WebUI 端口的 TCP 发送队列、主库只读统计与关键文件大小，
写成 JSONL 放在插件数据目录；/memdiag 回复摘要，/memdiag pack 把样本打成诊断包。

模块顶层与 __init__ 不做 I/O、不起任务：两版宿主在插件禁用或做配置检查时也会执行它们。
"""

from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional, Tuple, cast

import asyncio
import json
import os
import sqlite3
import sys
import threading
import time
import traceback

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase

from . import dbstats, report, storage
from .collector import PRIMARY_LABEL, Collector, Targets, WorkerGone, ensure_supported_platform, find_worker

PLUGIN_VERSION = "0.1.0"
FAST_STEM = "fast"
TREND_STEM = "trend"
COMMAND_PATTERN = r"^\s*/memdiag(?:\s+(?P<sub>pack))?\s*$"
# on_unload 的时间预算由同一 Runner 的所有插件串行共享（宿主关停时约 1.5 秒），每段等待都要很短。
STOP_WAIT_SEC = 0.4
HISTORY_HOURS = 26
# 每轮最多等 RPC 探测这么久；没回来就不等了，结果留到后面的轮次取。
PROBE_WAIT_SEC = 5.0
# 宿主默认 30 秒超时。探测要量的就是长卡顿，超时后重新发请求会把一次卡顿切成几段，趋势里只留下最后一段。
PROBE_TIMEOUT_MS = 600_000


class PluginSection(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    # 默认启用：这是用户为诊断专门安装的插件，默认禁用会让首次启动只生成配置、不采集。
    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="1.0.0", description="配置版本，由插件维护，请勿修改")


class CollectorSection(PluginConfigBase):
    """采集节拍。"""

    __ui_label__ = "采集"
    __ui_icon__ = "activity"
    __ui_order__ = 1

    fast_interval_sec: int = Field(default=15, ge=5, le=300, description="快速样本间隔（秒）：Worker 内存、WebUI 连接队列")
    trend_interval_sec: int = Field(default=300, ge=60, le=3600, description="趋势记录间隔（秒）：smaps、cgroup、文件大小、轻量数据库统计")
    db_interval_sec: int = Field(default=3600, ge=600, le=86400, description="数据库总量统计间隔（秒），会扫描整表")
    sql_limit_ms: int = Field(default=300, ge=50, le=5000, description="单条只读 SQL 的时限（毫秒），超时记为测量结果")
    max_consecutive_failures: int = Field(default=5, ge=1, le=100, description="同一类采样连续失败多少次后停止采集")


class StorageSection(PluginConfigBase):
    """样本文件与诊断包。"""

    __ui_label__ = "存储"
    __ui_icon__ = "hard-drive"
    __ui_order__ = 2

    max_file_mb: int = Field(default=8, ge=1, le=256, description="单个样本文件上限（MiB），写满后轮转")
    fast_backups: int = Field(default=4, ge=1, le=50, description="快速样本保留的旧文件个数（默认约两周）")
    trend_backups: int = Field(default=8, ge=1, le=100, description="趋势记录保留的旧文件个数（默认约数月）")
    keep_packs: int = Field(default=3, ge=1, le=20, description="诊断包保留个数")


class CommandSection(PluginConfigBase):
    """命令权限。"""

    __ui_label__ = "命令"
    __ui_icon__ = "terminal"
    __ui_order__ = 3

    allowed_users: List[str] = Field(
        default_factory=list,
        description="在宿主 operator 权限之上再收窄的白名单，每项格式为 平台:用户ID；为空表示不再收窄",
    )


class MemDiagConfig(PluginConfigBase):
    """内存诊断插件配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    collector: CollectorSection = Field(default_factory=CollectorSection)
    storage: StorageSection = Field(default_factory=StorageSection)
    command: CommandSection = Field(default_factory=CommandSection)


def instance_root_from(data_dir: Path) -> Path:
    """插件数据目录固定是 <实例根>/data/plugins/<插件 id>；dev 的实例目录可以与源码目录分离，所以不用 __file__ 推。"""
    if os.path.normcase(data_dir.parent.name) != "plugins" or os.path.normcase(data_dir.parent.parent.name) != "data":
        raise RuntimeError(f"插件数据目录不是 <实例根>/data/plugins/<id> 形式，无法确定实例根: {data_dir}")
    return data_dir.parents[2]


def locate_main_db(root: Path) -> Path:
    for candidate in (root / "data" / "MaiBot.db", Path.cwd() / "data" / "MaiBot.db"):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"找不到主库 MaiBot.db（已查 {root / 'data'} 与 {Path.cwd() / 'data'}）")


def amemorix_metadata_db(root: Path, raw_data_dir: str) -> Path:
    """与宿主 resolve_repo_path 一致：空值回落到 data/a-memorix，相对路径按实例根解析。"""
    value = raw_data_dir.strip()
    if not value:
        base = root / "data" / "a-memorix"
    else:
        candidate = Path(value).expanduser()
        base = candidate if candidate.is_absolute() else root / candidate
    return base.resolve() / "metadata" / "metadata.db"


def statistics_report_path(root: Path) -> Path:
    raw = os.environ.get("MAIBOT_STATISTICS_REPORT_PATH", "").strip() or "maibot_statistics.html"
    path = Path(raw)
    return path if path.is_absolute() else root / path


def load_history(trend_files: List[Path], worker_identity: List[Any], since: float) -> Tuple[List[Tuple[float, int]], int]:
    """从趋势文件恢复同一 Worker 进程的主指标历史（Runner 重启或插件重载后摘要不至于从零开始）。

    trend_files 从旧到新。从最新的往前读，读到记录早于 since 的文件就停：平时只读一个文件，刚轮转过时多读一个，
    避免在被测机器上一次解析几十 MiB 的历史。
    返回 (按时间升序的历史, 无法解析的行数)：磁盘写满或断电会留下半行，它不该让采集再也起不来，但要报出来。
    """
    history: List[Tuple[float, int]] = []
    broken = 0
    for trend_file in reversed(trend_files):
        points: List[Tuple[float, int]] = []
        earliest: Optional[float] = None
        with trend_file.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    broken += 1
                    continue
                earliest = record["t"] if earliest is None else min(earliest, record["t"])
                if record.get("k") != "trend" or record.get("wid") != worker_identity or record["t"] < since:
                    continue
                primary = record["window"]["primary_last"]
                if primary is not None:
                    points.append((record["t"], primary))
        history[:0] = points
        if earliest is not None and earliest <= since:
            break
    return history, broken


def config_snapshot(cfg: MemDiagConfig) -> Dict[str, Any]:
    """写进样本与诊断包的插件配置；白名单里是运营者的账号，只记条目数。"""
    data = cfg.model_dump()
    data["command"] = {"allowed_users_count": len(cfg.command.allowed_users)}
    return data


class RpcProbe:
    """测一次到 Worker 的 RPC 往返。

    请求在 Worker 主事件循环上处理，耗时反映主循环的阻塞程度。等不到结果时不取消请求，留到后面的轮次取：
    这样主循环卡了几十秒，最后记下的也是真实时长，而这一轮的进程外样本照常写盘。
    """

    def __init__(self, call: Callable[[], Awaitable[float]]) -> None:
        self._call = call
        self._task: Optional[asyncio.Task[float]] = None
        self._started = 0.0

    async def measure(self, wait_sec: float) -> Dict[str, Any]:
        if self._task is None:
            self._started = time.monotonic()
            self._task = asyncio.create_task(self._call(), name="memdiag.rpc_probe")
        done, _ = await asyncio.wait({self._task}, timeout=wait_sec)
        if not done:
            return {"rpc_ms": None, "rpc_pending_s": round(time.monotonic() - self._started, 1)}
        task, self._task = self._task, None
        exc = task.exception()
        if exc is not None:
            return {"rpc_ms": None, "rpc_error": repr(exc)}
        return {"rpc_ms": round(task.result(), 2)}

    def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None


class MemDiagPlugin(MaiBotPlugin):
    """内存诊断插件。"""

    config_model = MemDiagConfig

    def __init__(self) -> None:
        super().__init__()
        # 宿主把每个请求各开一个任务处理，配置更新可以与重载、卸载交错；启动与停止要串行，卸载要能作废进行中的启动。
        self._lifecycle = asyncio.Lock()
        self._closed = False
        self._task: Optional[asyncio.Task[None]] = None
        # 已发出停止信号、还没确认结束的任务；并发的卸载要等它，而不是看到 _task 为空就直接返回。
        self._stopping: Optional[asyncio.Task[None]] = None
        self._stop: Optional[asyncio.Event] = None
        self._cancel: Optional[threading.Event] = None
        self._pack_task: Optional[asyncio.Task[None]] = None
        self._pack_cancel: Optional[threading.Event] = None
        self._targets: Optional[Targets] = None
        # 采集实际在用的配置。配置变更失败时 self.cfg 已是新值，采集还在按旧值跑。
        self._running_cfg: Optional[MemDiagConfig] = None
        self._config_error: Optional[str] = None
        self._fast_sink: Optional[storage.RotatingJsonl] = None
        self._trend_sink: Optional[storage.RotatingJsonl] = None
        self._latest: Optional[Dict[str, Any]] = None
        self._last_trend: Optional[Dict[str, Any]] = None
        self._db_light: Optional[Dict[str, Any]] = None
        self._db_heavy: Optional[Dict[str, Any]] = None
        self._history: Deque[Tuple[float, int]] = deque()
        self._started_mono = 0.0
        self._ended_mono: Optional[float] = None
        self._state = "未启动"

    # ---- 生命周期 ----

    async def on_load(self) -> None:
        async with self._lifecycle:
            # 两版宿主重载失败回滚时，会对卸载过的同一个实例再调一次 on_load。
            self._closed = False
            targets = await self._resolve_targets()
            if self._closed:
                self.ctx.logger.info("内存诊断在启动途中被卸载，放弃这次启动")
                return
            self._start_collector(targets)

    async def on_unload(self) -> None:
        # 幂等；组件注册失败时宿主会在 on_load 从未执行的情况下调用它。不拿生命周期锁：
        # 锁里可能正有一次启动在等 RPC（最长 30 秒），卸载等不起；置位 _closed 让那次启动自行作废。
        self._closed = True
        await self._stop_collector()
        pack, self._pack_task = self._pack_task, None
        pack_cancel, self._pack_cancel = self._pack_cancel, None
        if pack is not None and not pack.done():
            if pack_cancel is not None:
                pack_cancel.set()
            pack.cancel()
            await asyncio.wait({pack}, timeout=STOP_WAIT_SEC)

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        # 两版宿主的 version 都传空串，不能依赖；scope 为 self 时 self.config 已是新值。
        if scope != "self":
            return
        async with self._lifecycle:
            if self._closed:
                return
            # 先解析目标再停旧采集：解析要发 RPC，Worker 主循环卡住时会超时，这时旧采集应照常运行。
            try:
                targets = await self._resolve_targets()
            except Exception as exc:
                self.ctx.logger.error(
                    "配置变更后解析采集目标失败，没有按新配置重启采集（原有采集不受影响）: %r\n%s", exc,
                    "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
                )
                # 只记类型：这段文字会进 /memdiag 回复，而找不到主库的异常文本带绝对路径；完整原因在上面的日志里。
                self._config_error = type(exc).__name__
                raise
            if self._closed:
                self.ctx.logger.info("内存诊断在配置变更途中被卸载，不再重启采集")
                return
            await self._stop_collector()
            if self._closed:
                return
            self._start_collector(targets)

    @property
    def cfg(self) -> MemDiagConfig:
        return cast(MemDiagConfig, self.config)

    # ---- 采集任务 ----

    def _start_collector(self, targets: Targets) -> None:
        if self._task is not None:
            raise RuntimeError("采集任务已在运行，拒绝重复启动")
        cfg = self.cfg
        data_dir = Path(self.ctx.paths.data_dir)
        max_bytes = cfg.storage.max_file_mb * 1024 * 1024
        self._targets = targets
        self._running_cfg, self._config_error = cfg, None
        self._fast_sink = storage.RotatingJsonl(data_dir, FAST_STEM, max_bytes, cfg.storage.fast_backups)
        self._trend_sink = storage.RotatingJsonl(data_dir, TREND_STEM, max_bytes, cfg.storage.trend_backups)
        self._latest = self._last_trend = self._db_light = self._db_heavy = None
        self._history = deque()
        self._started_mono, self._ended_mono = time.monotonic(), None
        # 每次启动新建 Event，停掉的旧任务看不到新的；create_task 放在最后，前面任何一步失败都不会留下任务。
        stop, cancel = asyncio.Event(), threading.Event()
        task = asyncio.create_task(self._run(cfg, targets, stop, cancel), name="memdiag.collector")
        task.add_done_callback(self._on_collector_done)
        self._stop, self._cancel, self._task = stop, cancel, task
        self._state = "运行中"
        self.ctx.logger.info(
            "内存诊断开始采集：Worker pid=%s，实例根 %s，样本目录 %s", targets.worker_pid, targets.instance_root, data_dir
        )

    async def _stop_collector(self) -> None:
        task, self._task = self._task, None
        stop, self._stop = self._stop, None
        cancel, self._cancel = self._cancel, None
        if task is not None and stop is not None and cancel is not None:
            # stop 让循环在等待处返回；cancel 打断线程里正在执行的查询，Runner 退出时才不必等它们跑完。
            stop.set()
            cancel.set()
            self._stopping = task
        else:
            # 另一次调用（配置变更）正在停它：卸载也要等它真的结束，Runner 才能干净退出，重载出的新实例也不会与它同写一组文件。
            task = self._stopping
            if task is None:
                return
        if not task.done():
            done, _ = await asyncio.wait({task}, timeout=STOP_WAIT_SEC)
            if not done:
                task.cancel()
                await asyncio.wait({task}, timeout=STOP_WAIT_SEC)
        if self._stopping is task:
            self._stopping = None
        if self._state == "运行中":
            failure = None if task.cancelled() or not task.done() else task.exception()
            if failure is not None:
                # 任务在停止之前已因异常结束，完成回调却还没轮到执行（等它执行时 _task 已被清空）：原因在这里记下。
                self._mark_failed(failure)
            else:
                self._state = "已停止（插件卸载或配置变更）"
        if self._ended_mono is None:
            self._ended_mono = time.monotonic()

    def _on_collector_done(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        self.ctx.logger.error(
            "内存诊断采集任务异常退出: %r\n%s", exc, "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        )
        if task is self._task:
            # 已被新一次启动取代的旧任务不改当前状态。
            self._mark_failed(exc)

    def _mark_failed(self, exc: BaseException) -> None:
        self._state = f"已停止（异常：{exc!r}）"
        self._ended_mono = time.monotonic()

    def _on_pack_done(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self.ctx.logger.error(
                "诊断包生成失败: %r\n%s", exc, "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            )

    async def _config_value(self, key: str, expected: type, timeout_ms: Optional[int] = None) -> Any:
        # 不传 default：键缺失时宿主返回 None，按类型校验直接报错，不用默认值掩盖。
        # 走 call_capability：ctx.config.get 是它的包装，但不接受超时。
        value = await self.ctx.call_capability("config.get", timeout_ms=timeout_ms, key=key)
        if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
            raise RuntimeError(f"宿主配置 {key} 不可用: {value!r}")
        return value

    async def _resolve_targets(self) -> Targets:
        ensure_supported_platform()
        data_dir = Path(self.ctx.paths.data_dir)
        root = instance_root_from(data_dir)
        if not os.path.samefile(root, os.getcwd()):
            # Runner 继承 Worker 的工作目录，两版都等于实例根；不一致多半是符号链接，记一笔继续按插件数据目录推。
            self.ctx.logger.warning("实例根 %s 与工作目录 %s 不一致，以插件数据目录推出的实例根为准", root, os.getcwd())
        webui_enabled = await self._config_value("webui.enabled", bool)
        webui_port = await self._config_value("webui.port", int) if webui_enabled else None
        amemorix_dir = await self._config_value("a_memorix.storage.data_dir", str)
        worker_pid, identity = await asyncio.to_thread(find_worker)
        main_db = await asyncio.to_thread(locate_main_db, root)
        logs = root / "logs"
        return Targets(
            worker_pid=worker_pid,
            worker_identity=identity,
            instance_root=root,
            webui_port=webui_port,
            watched_files={
                # 已发布的版本都是整份 JSON；JSONL 留给改成追加写的版本，迁移瞬间可能两个都在。
                "review_log_json": logs / "expression_review" / "review_logs.json",
                "review_log_jsonl": logs / "expression_review" / "review_logs.jsonl",
                # v1.3.13 起宿主自带的进程内内存诊断日志，1.2.3 没有。
                "memory_jsonl": logs / "memory_diagnostics" / "memory.jsonl",
                "statistics_report": statistics_report_path(root),
                "local_store": root / "data" / "local_store.json",
            },
            db_files={"main_db": main_db, "amemorix_db": amemorix_metadata_db(root, amemorix_dir)},
            watched_dirs={
                "app_logs": (logs, "app_*.log.jsonl"),
                "llm_cache_stats": (logs / "llm_cache_stats", "*"),
            },
        )

    def _remember(self, timestamp: float, primary: int) -> None:
        """追加一个主指标点，只保留最近 HISTORY_HOURS 小时；按时间而不是点数裁剪，改了趋势间隔也不丢恢复的历史。"""
        if self._history and timestamp < self._history[-1][0]:
            # 墙钟被往回拨过，旧点与新点无法再按时间比较。
            self.ctx.logger.warning("系统时钟回拨了 %.0f 秒，内存诊断摘要的增长历史已清空", self._history[-1][0] - timestamp)
            self._history.clear()
        self._history.append((timestamp, primary))
        while self._history[0][0] < timestamp - HISTORY_HOURS * 3600:
            self._history.popleft()

    async def _run(self, cfg: MemDiagConfig, targets: Targets, stop: asyncio.Event, cancel: threading.Event) -> None:
        probe = RpcProbe(self._probe_rpc_ms)
        try:
            await self._collect(cfg, targets, stop, cancel, probe)
        except dbstats.Cancelled:
            # 停止信号打断了线程里的读取，属于正常停止。
            return
        finally:
            probe.close()

    async def _collect(
        self, cfg: MemDiagConfig, targets: Targets, stop: asyncio.Event, cancel: threading.Event, probe: RpcProbe
    ) -> None:
        collector = Collector(targets)
        fast_sink, trend_sink = cast(storage.RotatingJsonl, self._fast_sink), cast(storage.RotatingJsonl, self._trend_sink)
        for sink in (fast_sink, trend_sink):
            removed, failed = await asyncio.to_thread(sink.remove_excess_backups)
            if removed:
                self.ctx.logger.info("按新的保留个数删除了 %d 个旧样本文件: %s", len(removed), [path.name for path in removed])
            for path, exc in failed:
                self.ctx.logger.warning("多出的旧样本文件 %s 删不掉，下次启动再删: %r", path.name, exc)
        # 先恢复历史再写 meta：这一条 meta 可能正好让趋势文件轮转。
        restored, broken = await asyncio.to_thread(
            load_history, trend_sink.files(), list(targets.worker_identity), time.time() - HISTORY_HOURS * 3600
        )
        if broken:
            self.ctx.logger.warning("趋势文件有 %d 行无法解析（多半是磁盘写满或断电留下的半行），已跳过", broken)
        for timestamp, primary in restored:
            self._remember(timestamp, primary)
        meta: Optional[Dict[str, Any]] = await asyncio.to_thread(
            collector.meta, {"plugin_version": PLUGIN_VERSION, "config": config_snapshot(cfg)}
        )
        tables = await asyncio.to_thread(
            dbstats.existing_tables, targets.db_files["main_db"], cfg.collector.sql_limit_ms, cancel=cancel
        )
        loop = asyncio.get_running_loop()
        next_fast = next_trend = loop.time()
        # 总量统计会扫整表，启动后先等一个趋势节拍，避开宿主启动期。
        next_db = loop.time() + cfg.collector.trend_interval_sec
        failures = {"fast": 0, "trend": 0, "db": 0}
        msg_max: Optional[int] = None
        probe_failing = False
        while True:
            wake = min(next_fast, next_trend, next_db)
            try:
                await asyncio.wait_for(stop.wait(), timeout=max(0.0, wake - loop.time()))
                return
            except TimeoutError:
                pass
            now = loop.time()
            kind = "fast"
            try:
                if now >= next_fast:
                    next_fast = now + cfg.collector.fast_interval_sec
                    sample = await asyncio.to_thread(collector.fast)
                    rpc = await probe.measure(min(PROBE_WAIT_SEC, cfg.collector.fast_interval_sec / 2))
                    if "rpc_error" in rpc and not probe_failing:
                        self.ctx.logger.warning("到 Worker 的 RPC 探测失败（样本照常记录）: %s", rpc["rpc_error"])
                    probe_failing = "rpc_error" in rpc
                    sample.update(rpc)
                    await asyncio.to_thread(self._commit_fast, collector, fast_sink, sample)
                    self._latest = sample
                    failures["fast"] = 0
                if now >= next_trend:
                    kind = "trend"
                    next_trend = now + cfg.collector.trend_interval_sec
                    if meta is not None:
                        # 写在循环里：轮转被占用或磁盘写满时与样本一样计入连续失败、下一轮重试，不让采集一启动就结束。
                        await asyncio.to_thread(trend_sink.append, meta)
                        meta = None
                    record = await asyncio.to_thread(
                        self._trend_round, collector, trend_sink, tables, msg_max, cfg.collector.sql_limit_ms, cancel
                    )
                    self._last_trend = record
                    self._db_light = record["db"]
                    current_max = record["db"].get("mai_messages_max_id")
                    if isinstance(current_max, int):
                        # 超时记为字典时保留上一轮的基线，下一轮继续按区间计数。
                        msg_max = current_max
                    if record["window"]["primary_last"] is not None:
                        self._remember(record["t"], record["window"]["primary_last"])
                    failures["trend"] = 0
                if now >= next_db:
                    kind = "db"
                    next_db = now + cfg.collector.db_interval_sec
                    self._db_heavy = await asyncio.to_thread(
                        self._db_round, targets, trend_sink, tables, cfg.collector.sql_limit_ms, cancel
                    )
                    failures["db"] = 0
            except dbstats.Cancelled:
                raise
            except WorkerGone as exc:
                self._state = f"已停止（{exc}）"
                self._ended_mono = time.monotonic()
                self.ctx.logger.warning("Worker 已不可用，内存诊断停止采集: %s", exc)
                await self._append_marker(trend_sink, {"t": round(time.time(), 3), "k": "stop", "reason": str(exc)})
                return
            except Exception as exc:  # 本任务的最外层：记录后继续，同一类连续失败达到上限则结束任务
                failures[kind] += 1
                self.ctx.logger.error(
                    "内存诊断 %s 采样失败（连续第 %d 次）: %r\n%s", kind, failures[kind], exc, traceback.format_exc()
                )
                await self._append_marker(
                    trend_sink,
                    {"t": round(time.time(), 3), "k": "error", "kind": kind, "error": repr(exc), "n": failures[kind]},
                )
                if failures[kind] >= cfg.collector.max_consecutive_failures:
                    raise

    async def _append_marker(self, sink: storage.RotatingJsonl, record: Dict[str, Any]) -> None:
        """写 stop / error 标记。写不进去多半与触发它的失败同因（磁盘写满），只记日志：
        否则这里的异常会绕过连续失败计数直接结束采集，或盖掉 Worker 退出的原因。"""
        try:
            await asyncio.to_thread(sink.append, record)
        except OSError as exc:
            self.ctx.logger.error("内存诊断写入 %s 标记失败: %r", record["k"], exc)

    async def _probe_rpc_ms(self) -> float:
        started = time.perf_counter()
        await self._config_value("webui.enabled", bool, timeout_ms=PROBE_TIMEOUT_MS)
        return (time.perf_counter() - started) * 1000.0

    @staticmethod
    def _commit_fast(collector: Collector, sink: storage.RotatingJsonl, sample: Dict[str, Any]) -> None:
        collector.commit_fast(sample)
        sink.append(sample)

    @staticmethod
    def _trend_round(
        collector: Collector,
        sink: storage.RotatingJsonl,
        tables: List[str],
        msg_max: Optional[int],
        limit_ms: int,
        cancel: threading.Event,
    ) -> Dict[str, Any]:
        record = collector.trend()
        record["db"] = dbstats.light_stats(collector.targets.db_files["main_db"], tables, msg_max, limit_ms, cancel=cancel)
        sink.append(record)
        collector.reset_window()
        return record

    @staticmethod
    def _db_round(
        targets: Targets, sink: storage.RotatingJsonl, tables: List[str], limit_ms: int, cancel: threading.Event
    ) -> Dict[str, Any]:
        stats = dbstats.heavy_stats(targets.db_files["main_db"], tables, limit_ms, cancel=cancel)
        sink.append({"t": round(time.time(), 3), "k": "db", "wid": list(targets.worker_identity), "stats": stats})
        return stats

    # ---- 命令 ----

    @Command(
        "memdiag",
        description="内存诊断：/memdiag 回复摘要，/memdiag pack 生成诊断包",
        pattern=COMMAND_PATTERN,
        permission="operator",
        intercept_message_level=1,
    )
    async def handle_memdiag(
        self,
        *,
        stream_id: str,
        matched_groups: Dict[str, Any],
        platform: str,
        user_id: str,
        is_local_operator: bool,
        **kwargs: Any,
    ) -> Tuple[bool, str, bool]:
        """宿主每次都传这些键，写成必填参数，缺了直接 TypeError；其余键（text、message 等）由 kwargs 接住。"""
        if not self._whitelisted(platform, user_id, is_local_operator):
            await self._reply(stream_id, f"你（{platform}:{user_id}）不在内存诊断插件的白名单 [command].allowed_users 内。")
            return True, "memdiag denied", True
        if matched_groups["sub"] == "pack":
            await self._reply(stream_id, self._start_pack(stream_id))
            return True, "memdiag pack", True
        await self._reply(stream_id, self._summary_text(include_dir=True))
        return True, "memdiag summary", True

    def _whitelisted(self, platform: str, user_id: str, is_local_operator: bool) -> bool:
        allowed = self.cfg.command.allowed_users
        if not allowed or is_local_operator:
            return True
        # 与宿主 build_scoped_user_id 一致：平台转小写，用户号只去空白（1.2.5 之前的宿主把整条转小写，大写用户号会匹配失败）。
        normalized = set()
        for entry in allowed:
            entry_platform, sep, entry_user = entry.strip().partition(":")
            if not sep or not entry_platform.strip() or not entry_user.strip():
                raise ValueError(f"[command].allowed_users 的条目应为 平台:用户ID，实际为 {entry!r}")
            normalized.add(f"{entry_platform.strip().lower()}:{entry_user.strip()}")
        return f"{platform.strip().lower()}:{user_id.strip()}" in normalized

    async def _reply(self, stream_id: str, text: str) -> None:
        # storage_message=False：诊断回复不写进消息库，也就不进入聊天上下文。
        ok = await self.ctx.send.text(text, stream_id, storage_message=False)
        if ok is not True:
            raise RuntimeError(f"向 {stream_id} 发送回复失败")

    def _display_path(self, path: Path) -> str:
        # 回复可能发在群里，只给相对实例根的路径：绝对路径里常带系统用户名。
        return os.path.relpath(path, cast(Targets, self._targets).instance_root)

    def _summary_text(self, *, include_dir: bool) -> str:
        targets = self._targets
        if targets is None:
            return f"内存诊断：采集{self._state}，没有可用的目标进程。"
        ended = time.monotonic() if self._ended_mono is None else self._ended_mono
        state = self._state if self._config_error is None else f"{self._state}（未按新配置重启：{self._config_error}，详见日志）"
        return report.format_summary(
            latest=self._latest,
            history=list(self._history),
            last_trend=self._last_trend,
            db_light=self._db_light,
            db_heavy=self._db_heavy,
            primary_label=PRIMARY_LABEL,
            worker_pid=targets.worker_pid,
            collector_state=state,
            sampled_seconds=ended - self._started_mono,
            fast_interval_sec=cast(MemDiagConfig, self._running_cfg).collector.fast_interval_sec,
            output_dir=self._display_path(Path(self.ctx.paths.data_dir)) if include_dir else None,
            webui_enabled=targets.webui_port is not None,
        )

    def _start_pack(self, stream_id: str) -> str:
        # 判断与 create_task 之间没有 await，在单线程事件循环里是原子的，不需要锁。
        if self._stop is None or self._targets is None:
            return "内存诊断插件未在运行或正在停止，无法生成诊断包"
        if self._pack_task is not None and not self._pack_task.done():
            return "已有诊断包在生成中，请稍候"
        self._pack_cancel = threading.Event()
        self._pack_task = asyncio.create_task(self._pack(stream_id, self._pack_cancel), name="memdiag.pack")
        self._pack_task.add_done_callback(self._on_pack_done)
        return "开始生成诊断包，完成后另行回复"

    async def _pack(self, stream_id: str, cancel: threading.Event) -> None:
        targets = cast(Targets, self._targets)
        cfg = cast(MemDiagConfig, self._running_cfg)
        summary = self._summary_text(include_dir=False)
        sinks = [sink for sink in (self._fast_sink, self._trend_sink) if sink is not None]
        try:
            path, stale = await asyncio.to_thread(
                self._build_pack_blocking, targets, sinks, summary, config_snapshot(cfg), cfg.collector.sql_limit_ms,
                cfg.storage.keep_packs, Path(self.ctx.paths.data_dir) / "packs", cancel,
            )
            size = await asyncio.to_thread(lambda: path.stat().st_size)
        except dbstats.Cancelled:
            # 只在卸载时发生，Runner 即将停止，不再回复。
            return
        except Exception as exc:
            await self._reply(stream_id, f"诊断包生成失败：{exc!r}")
            raise
        text = (
            f"诊断包已生成：{self._display_path(path)}（{report.human_bytes(size)}）。"
            "包内是内存样本、统计与本插件配置（白名单只记条目数），不含聊天内容与宿主配置。"
        )
        if stale:
            for old, exc in stale:
                self.ctx.logger.warning("旧诊断包 %s 删不掉，下次打包再删: %r", old.name, exc)
            text += f"另有 {len(stale)} 个旧包删不掉（可能正被别的程序打开），下次打包时再删。"
        await self._reply(stream_id, text)

    @staticmethod
    def _build_pack_blocking(
        targets: Targets,
        sinks: List[storage.RotatingJsonl],
        summary: str,
        config: Dict[str, Any],
        limit_ms: int,
        keep: int,
        out_dir: Path,
        cancel: threading.Event,
    ) -> Tuple[Path, List[Tuple[Path, OSError]]]:
        tables = dbstats.existing_tables(targets.db_files["main_db"], limit_ms, cancel=cancel)
        meta = {
            "plugin_version": PLUGIN_VERSION,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "host_version": os.environ.get("MAIBOT_HOST_VERSION"),
            "platform": sys.platform,
            "python": sys.version.split()[0],
            "sqlite": sqlite3.sqlite_version,
            "worker_pid": targets.worker_pid,
            "wid": list(targets.worker_identity),
            "summary": summary,
            "config": config,
            "db_heavy": dbstats.heavy_stats(targets.db_files["main_db"], tables, limit_ms, cancel=cancel),
        }
        memory_dir = targets.instance_root / "logs" / "memory_diagnostics"
        host_logs = sorted(memory_dir.glob("memory.jsonl*")) if memory_dir.is_dir() else []
        sources = {"samples": [path for sink in sinks for path in sink.files()], "host_memory_diagnostics": host_logs}
        return report.build_pack(out_dir, sources, meta, keep, cancel=cancel)


def create_plugin() -> MemDiagPlugin:
    return MemDiagPlugin()
