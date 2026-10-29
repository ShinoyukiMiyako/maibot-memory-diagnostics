import sqlite3
import threading
import time
from datetime import datetime

import pytest

from memdiag import dbstats

SCHEMA = """
CREATE TABLE mai_messages (id INTEGER PRIMARY KEY, raw_content BLOB);
CREATE TABLE llm_usage (id INTEGER PRIMARY KEY);
CREATE TABLE expressions (id INTEGER PRIMARY KEY);
CREATE TABLE images (id INTEGER PRIMARY KEY, image_type TEXT);
CREATE TABLE jargons (id INTEGER PRIMARY KEY, evidence_messages TEXT);
CREATE TABLE maisaka_monitor_events (id INTEGER PRIMARY KEY, payload_json TEXT NOT NULL);
CREATE TABLE statistics_message_hourly (id INTEGER PRIMARY KEY, bucket_time DATETIME NOT NULL, message_count INTEGER);
CREATE TABLE statistics_aggregation_cursors (source_name TEXT PRIMARY KEY, last_processed_id INTEGER);
"""
# 在一万条虚拟机指令一次的进度回调下，这条语句会触发几十次回调。
SLOW_ENOUGH = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 50000) SELECT COUNT(*) FROM c"


def make_db(path, messages=10, cursor=7):
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    connection.executemany("INSERT INTO mai_messages (id, raw_content) VALUES (?, ?)", [(i, b"x") for i in range(1, messages + 1)])
    connection.executemany("INSERT INTO llm_usage (id) VALUES (?)", [(i,) for i in range(1, 4)])
    connection.executemany("INSERT INTO images (image_type) VALUES (?)", [("IMAGE",), ("IMAGE",), ("EMOJI",)])
    connection.executemany("INSERT INTO jargons (evidence_messages) VALUES (?)", [("abc",), ("黑话",), (None,)])
    connection.executemany("INSERT INTO maisaka_monitor_events (payload_json) VALUES (?)", [("{}",), ("{\"a\":1}",)])
    connection.executemany(
        "INSERT INTO statistics_message_hourly (bucket_time, message_count) VALUES (?, ?)",
        [("2026-10-28 10:00:00", 5), ("2026-10-01 10:00:00", 2), ("2026-09-01 10:00:00", 100)],
    )
    connection.execute("INSERT INTO statistics_aggregation_cursors VALUES ('messages', ?)", (cursor,))
    connection.commit()
    connection.close()


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "MaiBot.db"
    make_db(path)
    return path


@pytest.fixture
def go():
    return threading.Event()


class StepClock:
    """每次读取前进固定步长的假时钟：超时与否只取决于调用次数，不受机器快慢影响。"""

    def __init__(self, step):
        self.step = step
        self.now = 0.0
        self.calls = 0

    def __call__(self):
        self.calls += 1
        self.now += self.step
        return self.now


def test_existing_tables_lists_schema(db, go):
    assert set(dbstats.existing_tables(db, 300, cancel=go)) >= {"mai_messages", "statistics_message_hourly", "jargons"}


def test_existing_tables_raises_when_listing_times_out(db, go, monkeypatch):
    monkeypatch.setattr(dbstats, "run_readonly", lambda *args, **kwargs: {"tables": {"timeout_ms": 5}})
    with pytest.raises(TimeoutError):
        dbstats.existing_tables(db, 5, cancel=go)


def test_light_stats_counts_rows_between_rounds(db, go):
    tables = dbstats.existing_tables(db, 300, cancel=go)
    first = dbstats.light_stats(db, tables, None, 300, cancel=go)
    assert first["mai_messages_max_id"] == 10
    assert first["mai_messages_min_id"] == 1
    assert first["llm_usage_max_id"] == 3
    assert first["expressions_max_id"] is None
    assert first["page_count"] > 0
    assert "mai_messages_delta" not in first
    second = dbstats.light_stats(db, tables, 4, 300, cancel=go)
    assert second["mai_messages_delta"] == 6
    deleted = dbstats.light_stats(db, tables, 50, 300, cancel=go)
    assert deleted["mai_messages_delta"] == {"deleted_tail": True}
    if dbstats.HAS_OCTET_LENGTH:
        assert first["monitor_events"] == [2, 4.5]


def test_heavy_stats_combines_aggregates_and_unaggregated_tail(db, go):
    tables = dbstats.existing_tables(db, 300, cancel=go)
    result = dbstats.heavy_stats(db, tables, 300, cancel=go, now=datetime(2026, 10, 29, 12, 0, 0))
    assert result["mai_messages_total"] == 10
    assert result["images_by_type"] == {"IMAGE": 2, "EMOJI": 1}
    # abc 3 字节，黑话 6 字节（UTF-8）；没有 octet_length 时 length 按字符计为 2。
    assert result["jargon_evidence_bytes"] == (9 if dbstats.HAS_OCTET_LENGTH else 5)
    # 30 天窗口只含 10-28 与 10-01 两个桶；游标 7 之后还有 3 行未聚合。
    assert result["msg_30d_approx"] == {"aggregated": 7, "tail": 3, "total": 10}


def test_heavy_stats_skips_tail_when_cursor_is_far_behind(tmp_path, go):
    path = tmp_path / "MaiBot.db"
    make_db(path, messages=3, cursor=0)
    connection = sqlite3.connect(path)
    connection.execute("INSERT INTO mai_messages (id) VALUES (?)", (dbstats.TAIL_COUNT_LIMIT + 10,))
    connection.commit()
    connection.close()
    result = dbstats.heavy_stats(path, dbstats.existing_tables(path, 300, cancel=go), 300, cancel=go, now=datetime(2026, 10, 29))
    assert result["msg_30d_approx"]["tail"] is None
    assert "未计" in result["msg_30d_approx"]["note"]


def test_slow_query_is_recorded_as_timeout(db, go):
    slow = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 100000000) SELECT COUNT(*) FROM c"
    assert dbstats.run_readonly(db, [("slow", slow, ())], 50, cancel=go) == {"slow": {"timeout_ms": 50}}


def test_total_row_counts_obey_the_time_limit(tmp_path, go, monkeypatch):
    # 不带 WHERE 的 COUNT(*) 在 B 树内部循环，进度回调一次也不触发；总数语句必须走逐行循环才受时限约束。
    path = tmp_path / "big.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE mai_messages (id INTEGER PRIMARY KEY)")
    connection.executemany("INSERT INTO mai_messages (id) VALUES (?)", ((i,) for i in range(1, 30001)))
    connection.commit()
    connection.close()
    monkeypatch.setattr(dbstats.time, "monotonic", StepClock(1.0))
    result = dbstats.heavy_stats(path, ["mai_messages"], 300, cancel=go)
    assert result["mai_messages_total"] == {"timeout_ms": 300}


def test_each_statement_gets_its_own_deadline(db, go, monkeypatch):
    calibrate = StepClock(0.0)
    monkeypatch.setattr(dbstats.time, "monotonic", calibrate)
    dbstats.run_readonly(db, [("one", SLOW_ENOUGH, ())], 1000, cancel=go)
    handler_calls = calibrate.calls - 1
    assert handler_calls >= 3
    # 每条语句的回调次数约为时限的三分之二：各自计时都不超时，若两条共用一个截止时间第二条必然超时。
    monkeypatch.setattr(dbstats.time, "monotonic", StepClock(1.0 / (1.5 * handler_calls)))
    result = dbstats.run_readonly(db, [("a", SLOW_ENOUGH, ()), ("b", SLOW_ENOUGH, ())], 1000, cancel=go)
    assert result["a"] == result["b"] == [(50000,)]


def test_cancel_before_start_raises(db):
    stop = threading.Event()
    stop.set()
    with pytest.raises(dbstats.Cancelled):
        dbstats.run_readonly(db, [("n", "SELECT 1", ())], 300, cancel=stop)


def test_cancel_between_short_statements_raises(db, monkeypatch):
    # 短语句不满一次进度回调的指令数，停止信号在两条语句之间到达时只能靠语句前的检查拦下。
    stop = threading.Event()

    def clock():
        stop.set()
        return 0.0

    monkeypatch.setattr(dbstats.time, "monotonic", clock)
    with pytest.raises(dbstats.Cancelled):
        dbstats.run_readonly(db, [("a", "SELECT 1", ()), ("b", "PRAGMA page_count", ())], 300, cancel=stop)


def test_cancel_reaches_the_unaggregated_tail_queries(db, monkeypatch):
    stop = threading.Event()
    tables = dbstats.existing_tables(db, 300, cancel=stop)
    original = dbstats.run_readonly
    batches = []

    def stop_after_first_batch(path, queries, limit_ms, *, cancel):
        batches.append([name for name, _, _ in queries])
        result = original(path, queries, limit_ms, cancel=cancel)
        stop.set()
        return result

    monkeypatch.setattr(dbstats, "run_readonly", stop_after_first_batch)
    with pytest.raises(dbstats.Cancelled):
        dbstats.heavy_stats(db, tables, 300, cancel=stop, now=datetime(2026, 10, 29, 12, 0, 0))
    # 主批查询之后的尾部计数用的是同一个停止信号，入口处就被拦下。
    assert batches[1] == ["max"]


def test_cancel_interrupts_a_running_query(db):
    stop = threading.Event()
    slow = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 1000000000) SELECT COUNT(*) FROM c"
    outcome = {}

    def run():
        try:
            dbstats.run_readonly(db, [("slow", slow, ())], 5000, cancel=stop)
            outcome["result"] = "finished"
        except dbstats.Cancelled:
            outcome["result"] = "cancelled"

    worker = threading.Thread(target=run)
    worker.start()
    time.sleep(0.1)
    started = time.monotonic()
    stop.set()
    worker.join(timeout=4)
    assert outcome == {"result": "cancelled"}
    assert time.monotonic() - started < 1.0


def test_other_sql_errors_propagate(db, go):
    with pytest.raises(sqlite3.OperationalError):
        dbstats.run_readonly(db, [("bad", "SELECT * FROM no_such_table", ())], 300, cancel=go)


def test_readonly_connection_cannot_write(db, go):
    with pytest.raises(sqlite3.OperationalError):
        dbstats.run_readonly(db, [("write", "DELETE FROM mai_messages", ())], 300, cancel=go)
    assert dbstats.scalar(dbstats.run_readonly(db, [("n", "SELECT COUNT(*) FROM mai_messages", ())], 300, cancel=go)["n"]) == 10


def test_each_readonly_layer_holds_on_its_own(tmp_path, go):
    # mode=ro：路径不存在时报错，不会在宿主目录里建出一个空库。
    missing = tmp_path / "data" / "MaiBot.db"
    missing.parent.mkdir()
    with pytest.raises(sqlite3.OperationalError):
        dbstats.run_readonly(missing, [("n", "SELECT 1", ())], 300, cancel=go)
    assert not missing.exists()
    # query_only：即使 URI 的只读模式失效，连接本身也拒绝写入。
    path = tmp_path / "MaiBot.db"
    make_db(path)
    assert dbstats.run_readonly(path, [("q", "PRAGMA query_only", ())], 300, cancel=go)["q"] == [(1,)]


def test_scalar_rejects_multi_row_results():
    with pytest.raises(ValueError):
        dbstats.scalar([(1,), (2,)])
    assert dbstats.scalar({"timeout_ms": 5}) == {"timeout_ms": 5}
