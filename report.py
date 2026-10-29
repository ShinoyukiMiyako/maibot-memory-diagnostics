"""/memdiag 摘要文字与诊断包。

诊断包只收本插件写的样本文件、元信息，以及宿主自带的内存诊断日志（v1.3.13 起才有），
不收聊天内容、宿主配置文件和宿主应用日志；由用户自己决定是否发给维护者，插件不上传。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import json
import os
import threading
import time
import zipfile

from .dbstats import raise_if_cancelled

GROWTH_SPANS_HOURS = (1, 6, 24)
PACK_PREFIX = "memdiag-"
# 实际跨度与标称时长相差超过这个比例时，在摘要里写出实际跨度（中间停过采集或刚恢复历史）。
SPAN_TOLERANCE = 0.25
# 快速样本超过这么多个间隔没有更新，就在摘要里注明样本时间。
STALE_FAST_INTERVALS = 3
# 遗留的 .partial 超过这个时长才清理：更新的那个可能还在被别的线程写。
PARTIAL_MAX_AGE_SEC = 3600


def human_bytes(value: Optional[float]) -> str:
    if value is None:
        return "未知"
    sign = "-" if value < 0 else ""
    size = abs(float(value))
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{sign}{size:.0f} {unit}" if unit == "B" else f"{sign}{size:.2f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


def growth(history: Sequence[Tuple[float, int]], now: float, hours: float) -> Optional[Tuple[int, float]]:
    """history 为 (时间戳, 主指标) 升序序列；取不晚于 now - hours 的最后一个点作起点。

    返回 (增长量, 起点到终点的实际秒数)；历史不够长返回 None。实际跨度交给调用方展示，
    中间停过采集时它会明显长于标称时长。
    """
    if not history:
        return None
    cutoff = now - hours * 3600
    start: Optional[Tuple[float, int]] = None
    for point in history:
        if point[0] <= cutoff:
            start = point
        else:
            break
    if start is None:
        return None
    end = history[-1]
    return end[1] - start[1], end[0] - start[0]


def _signed(value: int) -> str:
    return ("+" if value >= 0 else "") + human_bytes(value)


def _growth_text(history: Sequence[Tuple[float, int]], now: float, hours: int) -> str:
    result = growth(history, now, hours)
    if result is None:
        return f"近 {hours} 小时 历史不足"
    delta, span = result
    text = f"近 {hours} 小时 {_signed(delta)}"
    if abs(span - hours * 3600) > SPAN_TOLERANCE * hours * 3600:
        text += f"（实际跨度 {span / 3600:.1f} 小时）"
    return text


def format_summary(
    *,
    latest: Optional[Dict[str, Any]],
    history: Sequence[Tuple[float, int]],
    last_trend: Optional[Dict[str, Any]],
    db_light: Optional[Dict[str, Any]],
    db_heavy: Optional[Dict[str, Any]],
    primary_label: str,
    worker_pid: int,
    collector_state: str,
    sampled_seconds: float,
    fast_interval_sec: float,
    output_dir: Optional[str],
    webui_enabled: bool,
    now: Optional[float] = None,
) -> str:
    """output_dir 为 None 时不写样本目录（诊断包里的摘要不带路径）。"""
    now = time.time() if now is None else now
    lines = [f"内存诊断：Worker pid={worker_pid}，采集{collector_state}，本次已采样 {sampled_seconds / 3600:.1f} 小时"]
    if latest is None:
        lines.append("还没有采到样本。")
        if output_dir is not None:
            lines.append(f"样本目录：{output_dir}")
        return "\n".join(lines)
    worker = latest["worker"]
    age = now - latest["t"]
    stale = f"（样本采于 {age / 60:.0f} 分钟前）" if age > STALE_FAST_INTERVALS * fast_interval_sec else ""
    lines.append(
        f"Worker 当前 RSS {human_bytes(worker['rss'])}，{primary_label} {human_bytes(worker['primary'])}，"
        f"峰值 {human_bytes(worker['hwm'])}{stale}"
    )
    lines.append(f"{primary_label}增长：" + "，".join(_growth_text(history, now, hours) for hours in GROWTH_SPANS_HOURS))
    children = latest.get("children") or {}
    if children:
        parts = []
        for kind, info in sorted(children.items()):
            part = f"{kind} ×{info['n']} {human_bytes(info['rss'])}"
            if info.get("exiting"):
                part += f"（另有 {info['exiting']} 个正在退出）"
            parts.append(part)
        lines.append("子进程 RSS：" + "，".join(parts))
    lines.append(f"本插件所在 Runner RSS {human_bytes(latest['self']['rss'])}")
    webui = latest.get("webui")
    if not webui_enabled:
        lines.append("WebUI：未启用")
    elif webui is not None:
        if not webui["listening"]:
            lines.append("WebUI：端口未在监听")
        else:
            text = f"WebUI 连接 {webui['conns']} 条"
            if webui["stalled"]:
                text += f"，其中疑似停止读取 {webui['stalled']} 条（最大发送队列 {human_bytes(webui['tx_max'])}，零窗口探测中）"
            lines.append(text)
    if last_trend is not None:
        cgroup = last_trend.get("cgroup")
        if cgroup and cgroup.get("version"):
            if "unavailable" in cgroup:
                lines.append(f"cgroup v{cgroup['version']}：读不到内存用量（{cgroup['unavailable']}）")
            else:
                limit = cgroup["max"]
                oom_kill = cgroup["events"].get("oom_kill")
                lines.append(
                    f"cgroup v{cgroup['version']}：用量 {human_bytes(cgroup['current'])} / "
                    f"{'未设上限' if limit is None else human_bytes(limit)}，"
                    f"OOM 杀进程 {'未知' if oom_kill is None else oom_kill} 次"
                )
        files = last_trend.get("files") or {}
        sized = [f"{name} {human_bytes(info['size'])}" for name, info in files.items() if info is not None]
        if sized:
            lines.append("文件：" + "，".join(sized))
    if db_heavy is not None:
        approx = db_heavy.get("msg_30d_approx") or {}
        total = db_heavy.get("mai_messages_total")
        parts = []
        if isinstance(total, int):
            parts.append(f"消息总数 {total}")
        if "total" in approx:
            parts.append(f"近 30 天约 {approx['total']} 条（近似值）")
        if parts:
            lines.append("数据库：" + "，".join(parts))
    elif db_light is not None and isinstance(db_light.get("mai_messages_max_id"), int):
        lines.append(f"数据库：消息最大 id {db_light['mai_messages_max_id']}（总数统计尚未执行）")
    if output_dir is not None:
        lines.append(f"样本目录：{output_dir}")
    return "\n".join(lines)


def _pack_name(out_dir: Path) -> Path:
    # 用 UTC 命名：容器重建后时区变化不会让新包的名字比旧包“更早”。
    stem = f"{PACK_PREFIX}{time.strftime('%Y%m%d-%H%M%SZ', time.gmtime())}"
    path = out_dir / f"{stem}.zip"
    index = 1
    while path.exists():
        path = out_dir / f"{stem}-{index}.zip"
        index += 1
    return path


def build_pack(
    out_dir: Path, sources: Dict[str, Iterable[Path]], meta: Dict[str, Any], keep: int, *, cancel: threading.Event
) -> Tuple[Path, List[Tuple[Path, OSError]]]:
    """把 sources（{包内目录: 文件列表}）与 meta 打成 zip，只保留最新的 keep 个包。

    先写 .partial 再在同目录内改名：Docker 下插件数据目录是单独挂载的，跨设备改名会失败。
    失败或被停止时删掉 .partial 再抛出，避免残片绕过 keep 的清理一直占着磁盘；进程被强杀时删不了，
    留到之后某次打包按时长清理。返回 (新包, 删不掉的旧包及原因)：新包已经写好，旧包删不掉
    （Windows 上正被解压软件打开）不能让这次打包算作失败，由调用方告诉用户。
    """
    if keep < 1:
        raise ValueError(f"keep 至少为 1: {keep}")
    out_dir.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for leftover in out_dir.glob(f"{PACK_PREFIX}*.zip.partial"):
        if now - leftover.stat().st_mtime > PARTIAL_MAX_AGE_SEC:
            leftover.unlink()
    path = _pack_name(out_dir)
    partial = path.with_name(path.name + ".partial")
    try:
        with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("meta.json", json.dumps(meta, ensure_ascii=False, indent=2))
            for arc_dir, files in sources.items():
                for file in files:
                    raise_if_cancelled(cancel)
                    archive.write(file, f"{arc_dir}/{file.name}")
        os.replace(partial, path)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    # 按修改时间而不是文件名排序，并且无论如何都不删刚写出的包。
    others: List[Path] = sorted(
        (old for old in out_dir.glob(f"{PACK_PREFIX}*.zip") if old != path), key=lambda old: old.stat().st_mtime
    )
    failed: List[Tuple[Path, OSError]] = []
    for old in others[: max(0, len(others) - (keep - 1))]:
        try:
            old.unlink()
        except OSError as exc:
            failed.append((old, exc))
    return path, failed
