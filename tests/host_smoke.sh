#!/usr/bin/env bash
# 在官方镜像里加载插件，确认采集真的开始了。单元测试用的是替身，发现不了宿主改配置键或改进程结构这类问题。
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
[ -s "$trend" ] || fail "没有写出趋势记录"

# 容器里的 Python 版本才是插件的运行环境；样本要有真实读数，只写出文件不算过。
docker exec -i "$name" /MaiMBot/.venv/bin/python - <<'PY'
import json
import time
from pathlib import Path

base = Path("/MaiMBot/data/plugins/shinoyuki.memdiag")
deadline = time.monotonic() + 30
while not (base / "fast.jsonl").exists():
    assert time.monotonic() < deadline, "等不到快速样本"
    time.sleep(1)
records = [json.loads(line) for line in (base / "trend.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
assert records[0]["k"] == "meta", records[0]
assert records[0]["webui_port"] is None or records[0]["webui_port"] > 0
sample = json.loads((base / "fast.jsonl").read_text(encoding="utf-8").splitlines()[0])
assert sample["worker"]["primary"] > 0 and sample["worker"]["rss"] > 0, sample
assert "plugin_runner" in sample["children"], sample["children"]
print("PASS", records[0]["host_version"], records[0]["python"], "webui_port", records[0]["webui_port"], "worker", records[0]["worker_pid"])
PY
