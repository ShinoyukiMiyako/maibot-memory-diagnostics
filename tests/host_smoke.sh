#!/usr/bin/env bash
# 在官方镜像里加载插件，确认采集真的开始了。单元测试用的是替身，发现不了宿主改配置键、表结构或进程结构这类问题。
# 用法（在仓库根目录）: bash tests/host_smoke.sh sengokucola/maibot:latest
set -euo pipefail

image=$1
name=memdiag-smoke-$$
work=$(mktemp -d)
plugin=$work/plugins/maibot-memory-diagnostics
mkdir -p "$plugin" "$work/config" "$work/data" "$work/plugin-data" "$work/logs"
cp _manifest.json ./*.py LICENSE "$plugin/"

cleanup() {
    docker rm -f "$name" >/dev/null 2>&1 || true
    # 容器里写出的文件属于 root。
    docker run --rm -v "$work:/work" --entrypoint sh "$image" -c 'rm -rf /work/*' >/dev/null 2>&1 || true
    rmdir "$work" 2>/dev/null || true
}
trap cleanup EXIT

docker pull -q "$image" >/dev/null
# 协议变更时哈希会变，取镜像自带 compose 里的值，不写死。
agreement() {
    docker run --rm --entrypoint sh "$image" -c "sed -n 's/^ *- $1=\([0-9a-f]*\).*/\1/p' docker-compose.yml | head -1"
}
docker run -d --name "$name" -e "EULA_AGREE=$(agreement EULA_AGREE)" -e "PRIVACY_AGREE=$(agreement PRIVACY_AGREE)" \
    -e MAIBOT_LEGACY_0X_UPGRADE_CONFIRMED=1 -e HTTP_HOST=0.0.0.0 \
    -v "$work/config:/MaiMBot/config" -v "$work/data:/MaiMBot/data" -v "$work/plugin-data:/MaiMBot/data/plugins" \
    -v "$work/plugins:/MaiMBot/plugins" -v "$work/logs:/MaiMBot/logs" "$image" >/dev/null

logs() { docker logs "$name" 2>&1 | sed 's/\x1b\[[0-9;]*m//g'; }

# 不用 grep -q：它匹配到就退出，logs 收到 SIGPIPE，pipefail 下整条管道算失败。
started() { logs | grep '内存诊断开始采集' >/dev/null; }

fail() {
    echo "FAIL: $1" >&2
    logs | tail -40 >&2
    exit 1
}

trend=$work/plugin-data/shinoyuki.memdiag/trend.jsonl
for _ in $(seq 1 60); do
    # 不能只匹配“失败”：成功时宿主也会打印“已加载=1（shinoyuki.memdiag），失败=0”。
    if logs | grep -E '插件注册失败: shinoyuki\.memdiag|shinoyuki\.memdiag on_load 失败' >&2; then
        fail "插件加载失败"
    fi
    if [ "$(docker inspect -f '{{.State.Running}}' "$name")" != true ]; then
        fail "容器已退出"
    fi
    if [ -s "$trend" ] && started; then
        break
    fi
    sleep 3
done
started || fail "等不到采集开始"
[ -s "$trend" ] || fail "趋势文件是空的"

# 容器里的 Python 版本才是插件的运行环境；样本要有真实读数，只写出文件不算过。
docker exec -i "$name" /MaiMBot/.venv/bin/python - <<'PY' || fail "容器内检查失败"
import importlib.util
import json
import threading
import time
from pathlib import Path

base = Path("/MaiMBot/data/plugins/shinoyuki.memdiag")


def records(name):
    path = base / name
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    return [json.loads(line) for line in text.splitlines()]


def wait_for(found, seconds, message):
    deadline = time.monotonic() + seconds
    while not found():
        assert time.monotonic() < deadline, message
        time.sleep(1)


wait_for(lambda: records("fast.jsonl"), 30, "等不到快速样本")
# meta 在第一轮趋势之前写出，趋势轮失败时文件里只有 meta 和 error。
wait_for(lambda: any(record["k"] in ("trend", "error") for record in records("trend.jsonl")), 30, "等不到趋势轮结果")
trend = records("trend.jsonl")
meta = trend[0]
assert meta["k"] == "meta", meta
assert meta["webui_port"] is None or meta["webui_port"] > 0
assert not [record for record in trend if record["k"] == "error"], trend
db = next(record for record in trend if record["k"] == "trend")["db"]
# 宿主改表名时这些键会被悄悄跳过，改列名时上面的 error 会拦下。
assert db["page_count"] > 0 and {"mai_messages_max_id", "llm_usage_max_id", "expressions_max_id"} <= db.keys(), db

sample = records("fast.jsonl")[0]
assert sample["worker"]["primary"] > 0 and sample["worker"]["rss"] > 0, sample
assert "plugin_runner" in sample["children"], sample["children"]
# 监听状态按 Worker 持有的 socket 判定，认错 Worker 或读错端口时一直是 False。
# 不能只看第一条：1.2.3 的 WebUI 在插件加载之后才启动。
if meta["webui_port"] is not None:
    wait_for(lambda: any(sample["webui"]["listening"] for sample in records("fast.jsonl")), 120, "Worker 没有监听 WebUI 端口")

# 总量统计要等 trend_interval_sec 才第一次跑，冒烟等不到。列名对不上时打包直接失败，约 4 小时后采集也会停；空库上就能发现。
spec = importlib.util.spec_from_file_location("dbstats", "/MaiMBot/plugins/maibot-memory-diagnostics/dbstats.py")
dbstats = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dbstats)
main_db = Path("/MaiMBot/data/MaiBot.db")
cancel = threading.Event()
stats = dbstats.heavy_stats(main_db, dbstats.existing_tables(main_db, 5000, cancel=cancel), 5000, cancel=cancel)
expected = {"mai_messages_total", "llm_usage_total", "expressions_total", "images_by_type", "jargon_evidence_bytes", "msg_30d_approx"}
assert expected <= stats.keys(), stats
print("PASS", meta["host_version"], meta["python"], "webui_port", meta["webui_port"], "worker", meta["worker_pid"])
PY
