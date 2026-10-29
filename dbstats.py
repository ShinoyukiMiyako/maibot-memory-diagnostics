"""主库只读统计。

在插件 Runner 里直接以只读 URI 打开 SQLite，不走宿主的数据库能力：后者在 Worker 主循环里同步执行，
不带 limit 时还会在 Worker 里物化整表，等于让被测进程替我们分配内存。

每轮打开、读完即关；每条语句都有硬时限。只读连接留着没读完的游标时，WebUI 手动 VACUUM 之类的
TRUNCATE checkpoint 会被它挡住，连带让 Worker 写入报 database is locked（Worker 的 busy_timeout 只有 1 秒）。
不读取任何文本列的内容。

时限与停止都靠进度回调打断语句。插件卸载时 Runner 进程要等线程池里的线程结束才能退出，
宿主只等约 1.5 秒，所以停止信号必须能打断正在执行的查询，而不只是不再等它。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import sqlite3
import threading
import time

# octet_length 从 SQLite 3.43 起才有；更早的版本用 length()，单位从字节变成字符。
HAS_OCTET_LENGTH = sqlite3.sqlite_version_info >= (3, 43, 0)
BYTE_LENGTH = "octet_length" if HAS_OCTET_LENGTH else "length"
PROGRESS_STEPS = 10000
# 统计汇总表之后的未聚合尾巴超过这个行数就不数了，避免退化成全表扫描。
TAIL_COUNT_LIMIT = 50000

Query = Tuple[str, str, Tuple[Any, ...]]


class Cancelled(Exception):
    """停止信号打断了正在进行的读取；这一轮没有完整结果，调用方不应写盘。"""


def raise_if_cancelled(cancel: threading.Event) -> None:
    if cancel.is_set():
        raise Cancelled("采集正在停止")


def count_all(table: str) -> str:
    # 不带 WHERE 的 COUNT(*) 会被编译成一条在 B 树内部循环的指令，期间不回到虚拟机，进度回调一次也不触发，
    # 时限与停止信号都打断不了它；加上恒真条件改走逐行循环，代价是慢一些。
    return f"SELECT COUNT(*) FROM {table} WHERE 1"


def run_readonly(db: Path, queries: Sequence[Query], limit_ms: int, *, cancel: threading.Event) -> Dict[str, Any]:
    """依次执行只读查询，返回 {名称: 结果行列表}；超时的查询结果记为 {"timeout_ms": limit_ms}。

    cancel 置位后，正在执行的语句在下一次进度回调时被打断，并抛出 Cancelled。
    """
    raise_if_cancelled(cancel)
    uri = db.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=2.0)
    try:
        connection.execute("PRAGMA query_only=1")
        connection.execute("PRAGMA cache_size=-1024")
        results: Dict[str, Any] = {}
        for name, sql, params in queries:
            raise_if_cancelled(cancel)
            deadline = time.monotonic() + limit_ms / 1000.0
            connection.set_progress_handler(
                lambda deadline=deadline: 1 if cancel.is_set() or time.monotonic() > deadline else 0, PROGRESS_STEPS
            )
            try:
                results[name] = connection.execute(sql, params).fetchall()
            except sqlite3.OperationalError as exc:
                if str(exc) != "interrupted":
                    raise
                raise_if_cancelled(cancel)
                # 超时是测量结果（库太大或被锁），不是错误。
                results[name] = {"timeout_ms": limit_ms}
        return results
    finally:
        connection.close()


def scalar(rows: Any) -> Any:
    """单行单列结果取值；超时标记原样返回。"""
    if isinstance(rows, dict):
        return rows
    if len(rows) != 1 or len(rows[0]) != 1:
        raise ValueError(f"期望单行单列，实际为 {rows!r}")
    return rows[0][0]


def existing_tables(db: Path, limit_ms: int, *, cancel: threading.Event) -> List[str]:
    rows = run_readonly(db, [("tables", "SELECT name FROM sqlite_master WHERE type = 'table'", ())], limit_ms,
                        cancel=cancel)["tables"]
    if isinstance(rows, dict):
        raise TimeoutError(f"读取表清单超时（{limit_ms} ms）")
    return [row[0] for row in rows]


def light_stats(
    db: Path, tables: Sequence[str], previous_msg_max: Optional[int], limit_ms: int, *, cancel: threading.Event
) -> Dict[str, Any]:
    """趋势节拍执行：主键 MIN/MAX、主键区间计数与文件页统计，代价与表大小无关或有上界。"""
    queries: List[Query] = [
        ("page_size", "PRAGMA page_size", ()),
        ("page_count", "PRAGMA page_count", ()),
        ("freelist_count", "PRAGMA freelist_count", ()),
        ("user_version", "PRAGMA user_version", ()),
    ]
    # MIN 与 MAX 必须分成两条：写在同一条语句里 SQLite 会退化成全索引扫描。
    for table in ("mai_messages", "llm_usage", "expressions"):
        if table in tables:
            queries.append((f"{table}_max_id", f"SELECT MAX(id) FROM {table}", ()))
    if "mai_messages" in tables:
        queries.append(("mai_messages_min_id", "SELECT MIN(id) FROM mai_messages", ()))
    if HAS_OCTET_LENGTH and "maisaka_monitor_events" in tables:
        # 监控事件表约 1 万行，有上界。
        queries.append(("monitor_events", f"SELECT COUNT(*), AVG({BYTE_LENGTH}(payload_json)) FROM maisaka_monitor_events", ()))
    raw = run_readonly(db, queries, limit_ms, cancel=cancel)
    result: Dict[str, Any] = {name: scalar(raw[name]) for name, _, _ in queries if name != "monitor_events"}
    if "monitor_events" in raw:
        result["monitor_events"] = raw["monitor_events"] if isinstance(raw["monitor_events"], dict) else list(raw["monitor_events"][0])
    current_max = result.get("mai_messages_max_id")
    if previous_msg_max is not None and isinstance(current_max, int):
        if current_max < previous_msg_max:
            # 没有 AUTOINCREMENT，WebUI 删除尾部消息后 MAX(id) 会回落。
            result["mai_messages_delta"] = {"deleted_tail": True}
        else:
            delta_rows = run_readonly(db, [("delta", "SELECT COUNT(*) FROM mai_messages WHERE id > ? AND id <= ?",
                                            (previous_msg_max, current_max))], limit_ms, cancel=cancel)["delta"]
            result["mai_messages_delta"] = scalar(delta_rows)
    return result


def heavy_stats(
    db: Path, tables: Sequence[str], limit_ms: int, *, cancel: threading.Event, now: Optional[datetime] = None
) -> Dict[str, Any]:
    """低频与打包时执行：总行数、近 30 天消息近似值、图片记录与黑话证据体量，会扫描整表或整个索引。"""
    now = datetime.now() if now is None else now
    queries: List[Query] = []
    for table in ("mai_messages", "llm_usage", "expressions", "images", "jargons"):
        if table in tables:
            queries.append((f"{table}_total", count_all(table), ()))
    if "images" in tables:
        # images 按哈希去重，只能叫“图片记录数”，不等于图片消息数。
        queries.append(("images_by_type", "SELECT image_type, COUNT(*) FROM images GROUP BY image_type", ()))
    if "jargons" in tables:
        queries.append(("jargon_evidence_bytes", f"SELECT COALESCE(SUM({BYTE_LENGTH}(evidence_messages)), 0) FROM jargons", ()))
    if not HAS_OCTET_LENGTH and "maisaka_monitor_events" in tables:
        queries.append(("monitor_events", f"SELECT COUNT(*), AVG({BYTE_LENGTH}(payload_json)) FROM maisaka_monitor_events", ()))
    has_aggregates = "statistics_message_hourly" in tables and "statistics_aggregation_cursors" in tables
    if has_aggregates:
        since = (now - timedelta(days=30)).strftime("%Y-%m-%d %H:00:00")
        queries.append(("msg_30d_aggregated", "SELECT COALESCE(SUM(message_count), 0) FROM statistics_message_hourly WHERE bucket_time >= ?", (since,)))
        queries.append(("aggregation_cursor", "SELECT last_processed_id FROM statistics_aggregation_cursors WHERE source_name = 'messages'", ()))
    raw = run_readonly(db, queries, limit_ms, cancel=cancel)
    result: Dict[str, Any] = {}
    for name, _, _ in queries:
        rows = raw[name]
        if name == "images_by_type":
            result[name] = rows if isinstance(rows, dict) else {str(kind): count for kind, count in rows}
        elif name == "monitor_events":
            result[name] = rows if isinstance(rows, dict) else list(rows[0])
        elif name == "aggregation_cursor":
            result[name] = rows if isinstance(rows, dict) else (rows[0][0] if rows else None)
        else:
            result[name] = scalar(rows)
    if has_aggregates:
        result["msg_30d_approx"] = _msg_30d(db, result, limit_ms, cancel)
    return result


def _msg_30d(db: Path, result: Dict[str, Any], limit_ms: int, cancel: threading.Event) -> Dict[str, Any]:
    """近 30 天消息数近似值 = 汇总表 30 天合计 + 汇总游标之后尚未聚合的行数。

    只是近似：两版的分桶字段不同，汇总排除了没有群号也没有用户号的行，WebUI 删除消息后汇总表也不回减。
    """
    aggregated = result["msg_30d_aggregated"]
    cursor = result["aggregation_cursor"]
    if isinstance(aggregated, dict) or isinstance(cursor, dict):
        return {"skipped": "超时"}
    if cursor is None:
        return {"aggregated": aggregated, "tail": None, "note": "汇总游标不存在，未计未聚合部分"}
    max_rows = run_readonly(db, [("max", "SELECT MAX(id) FROM mai_messages", ())], limit_ms, cancel=cancel)["max"]
    max_id = scalar(max_rows)
    if isinstance(max_id, dict) or max_id is None or max_id - cursor > TAIL_COUNT_LIMIT:
        return {"aggregated": aggregated, "tail": None, "note": f"未聚合部分超过 {TAIL_COUNT_LIMIT} 行或无法取得，未计"}
    tail_rows = run_readonly(db, [("tail", "SELECT COUNT(*) FROM mai_messages WHERE id > ?", (cursor,))], limit_ms,
                             cancel=cancel)["tail"]
    tail = scalar(tail_rows)
    if isinstance(tail, dict):
        return {"aggregated": aggregated, "tail": None, "note": "未聚合部分计数超时"}
    return {"aggregated": aggregated, "tail": tail, "total": aggregated + tail}
