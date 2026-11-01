# MaiBot 内存诊断插件

在 MaiBot 长时间运行、内存一路上涨直到被 OOM 杀掉的场景下，从插件里持续记录“内存是怎么涨上去的”，
出事后把诊断包交给维护者或自己分析。

插件运行在宿主拉起的插件 Runner 子进程里，看不到 Worker（真正跑聊天逻辑的进程）的 Python 堆，
所以它只做进程外的观测：读 `/proc`（Linux）、Windows 进程 API 或 macOS 的 libproc、cgroup、WebUI 端口的 TCP 发送队列、
主库的只读统计和几个已知会变大的文件。它回答的是“哪一类内存在涨、和什么同步涨”，
不回答“是哪个 Python 对象在涨”；后者需要宿主自带的进程内诊断（v1.3.13 起的 `logs/memory_diagnostics`）
或 memray / tracemalloc 这类工具。

只用 Python 标准库，不引入任何第三方依赖。

## 内存有问题时怎么反馈

MaiBot 运行几天到几十天后内存一路上涨、被 OOM 杀掉或被容器反复重启时，按下面做。插件从安装时才开始记录，
越早装，看到的走势越完整。

1. 安装插件，见[安装](#安装)。
2. 放行 `/memdiag`，见[命令权限](#命令权限)。默认谁都不能用，这是宿主对受保护命令的规定。
3. 照常使用，不用改配置。Worker 被杀后样本不会丢，重启后自动接着记录。
4. 内存涨上去或 OOM 之后，向麦麦发 `/memdiag pack`，稍等会收到“诊断包已生成”。消息里只能有这串文字：
   @ 麦麦或引用别的消息时，宿主不把它当命令，只当普通聊天。
5. 取走诊断包 `memdiag-*.zip`：

   | 部署方式 | 诊断包位置 |
   | --- | --- |
   | Docker（官方 compose） | `docker-compose.yml` 所在目录下的 `data/MaiMBot-plugin-data/shinoyuki.memdiag/packs/` |
   | 直接运行 | `<实例根>/data/plugins/shinoyuki.memdiag/packs/` |

   Docker 下回复里的路径 `data/plugins/...` 是容器内路径，到宿主机上按上表找。WebUI 下载不了诊断包。
6. 到本仓库的 Issues 选“内存问题反馈”，把诊断包拖进去，写明运行了多久、内存涨到多少。

   Docker 部署再附上重启次数，OOM 后容器恢复运行时 `OOMKilled` 会变回 `false`，这个数不会：

   ```bash
   docker inspect --format '{{.RestartCount}}' maim-bot-core
   ```

Issue 是公开的。诊断包里是内存读数、文件大小、各表行数与消息量、版本信息和本插件配置，以及 v1.3.13 起宿主自带的
内存诊断日志；没有聊天内容、API Key、客户端 IP 和宿主配置文件的内容，宿主配置只记 WebUI 端口号这类数值。
`cgroup.path` 可能带容器 ID 或 systemd 单元名。里面都是 JSON 文本，可以解压看过再发。

## 兼容性

| 项目 | 范围 |
| --- | --- |
| MaiBot 宿主 | 1.2.3 至 1.3.x |
| maibot-plugin-sdk | 2.8.0 及以上 |
| Linux | 全部功能；4.5 之前的内核从 `statm` 推算匿名内存，4.14 之前的内核没有 `smaps_rollup`（记为 `null`） |
| Windows | 进程内存、子进程、文件大小、数据库统计照常；没有 smaps、glibc 堆估计、cgroup、fd、线程数、正在退出的进程数与全部 WebUI 连接信息，另记句柄数 `handles` |
| macOS | 进程内存（主指标是物理占用 `phys_footprint`，峰值也按它算）、线程数、子进程、文件大小、数据库统计照常；没有 smaps、glibc 堆估计、cgroup、fd、正在退出的进程数与全部 WebUI 连接信息 |
| 其他 | 不支持，插件加载时直接报错 |

真实宿主上的端到端验证只在 Linux 上做过：官方 Docker 镜像 1.2.3、1.3.8、1.3.17、1.3.19（含容器 OOM 后恢复采集），
以及直接运行的 1.2.3。其中 ZIP 安装与放行走的是 WebUI 的接口，没有点过页面。Windows 与 macOS 上跑过采集代码的全部测试
（macOS 为 GitHub Actions 的 arm64 与 Intel 机器，读的是真实进程），没有在真实宿主里运行过。

macOS 上用 lldb 附加插件 Runner 时，Runner 会停住直到 `continue`，这段时间没有样本；停住约 35 秒起，
宿主的健康检查可能重启 Runner。继续运行后采集照常。附加期间 Runner 的父进程号是调试器，认不出 Worker：
按新配置重启会报错，原来的采集按旧配置继续；重载插件则会先停掉采集，再因加载失败把插件卸掉。脱离后重载插件即可恢复。

## 安装

先下载本仓库：页面上的 Code → Download ZIP。

- WebUI（1.3.3 起）：在侧栏“插件扩展”页点右上角的“更多操作”（…），选“从 ZIP 安装插件”，上传这个 ZIP，不用解压，然后按提示重启麦麦。
- 手动（所有版本）：解压后把目录放进插件目录，目录里要直接有 `_manifest.json` 与 `plugin.py`，然后重启麦麦。
  插件目录直接运行时是 `<实例根>/plugins/`，Docker（官方 compose）是 `docker-compose.yml` 所在目录下的 `data/MaiMBot/plugins/`。

宿主会在插件目录里生成 `config.toml`，插件默认启用，加载后立即开始采集。
日志里出现 `内存诊断开始采集：Worker pid=...` 即表示正常运行，WebUI 的日志页也能看到。

### 命令权限

`/memdiag` 是宿主意义上的受保护（operator）命令，宿主按下面任一条放行：

- 宿主自带的本地操作员终端（`bot_console` 平台）不受放行规则限制，但要 `[debug].enable_console_input = true`
  （1.3.0 起默认开，1.2.x 默认关）且有交互式终端，Docker 默认没有。
- 只放行本命令（推荐）：在 WebUI“麦麦设置”页的“命令管理”标签里选 `/memdiag`（插件 `shinoyuki.memdiag`，可搜 `memdiag`），
  填写“仅为此命令放行用户”，保存后立即生效，不用重启。

  也可以手改 MaiBot 的 `bot_config.toml`（Docker 是 `docker-compose.yml` 所在目录下的 `docker-config/mmc/bot_config.toml`）。
  `[plugin]` 段已经有一行 `command_permissions = {}`，要改写这一行，不能另起 `[plugin.command_permissions...]` 表头：
  同一个键写两次时配置解析失败，MaiBot 下次重启就起不来。

  ```toml
  command_permissions = { "shinoyuki.memdiag.memdiag" = { allow_users = ["qq:123456789"] } }
  ```

  这一行已有别的命令时，优先用上面的 WebUI 改。要手改就把新条目加进最外层花括号，和已有条目用逗号隔开，
  不要换行，末尾不留逗号。

  用户写成 `平台:账号`。想在 WebUI 的聊天室里发命令，v1.3.16 起填 `internal:owner`；更早的版本聊天室账号每个浏览器不同，
  建议放行聊天平台上的账号，在那边发。

  同一处还有 `allow_chats`（聊天流 ID）。放行聊天后，该聊天里的任何人都能执行本命令，包括 `/memdiag pack`。
- `bot_config.toml` 的 `[plugin].permission`：列在这里的用户能执行所有受保护命令，包括用 `/pm` 加载、重载插件，
  读取和修改任意插件配置（含本插件的 `[command].allowed_users`），以及用 `/offline` 关闭全部适配器，远多于使用本命令所需。

想在宿主放行之上再收窄（例如放行了整个群），可以在本插件的 `config.toml` 里设置
`[command].allowed_users = ["qq:123456789"]`；留空表示不再收窄。条目格式是 `平台:用户ID`，
平台不区分大小写，用户 ID 区分大小写；格式不对的条目会让命令直接报错，而不是被悄悄忽略。

1.2.5 之前的宿主把 `permission` 与 `allow_users` 的整条条目转成小写再比对，用户 ID 含大写字母
（例如部分平台的 openid）时放行不了；1.2.5 起只把平台转小写。

## 命令

| 命令 | 作用 |
| --- | --- |
| `/memdiag` | 回复当前摘要：Worker 内存、近 1/6/24 小时增长、子进程、WebUI 连接、cgroup、关键文件大小、消息量 |
| `/memdiag pack` | 在后台打一个诊断包，完成后另发一条消息告知路径与大小 |

回复都不写入消息库，不进入聊天上下文。回复可能发在群里，所以其中的路径都相对实例根目录，不带绝对路径。

## 采集内容

三档节拍，都可在 `config.toml` 的 `[collector]` 里调整：

| 档位 | 默认间隔 | 内容 |
| --- | --- | --- |
| 快速样本 `fast` | 15 秒 | Worker 的 RSS、匿名内存（Windows 为提交内存，macOS 为物理占用）、峰值、线程数（Windows 为句柄数）；按可执行文件名汇总的子进程内存（Linux 上插件 Runner、Chromium 各归一类，另计正在退出的进程数；Windows 与 macOS 按小写的文件名分桶，Runner 算在解释器名下）；Runner 自身内存；WebUI 端口上 Worker 的连接数、发送队列、疑似停止读取的连接数；一次到 Worker 的 RPC 往返耗时 |
| 趋势记录 `trend` | 5 分钟 | 自上一条趋势记录以来快速样本的最小/最大/末值（上一轮没写成时并进这一条，`window.samples` 相应变多）；`smaps_rollup`；glibc 线程堆估计；fd 分类与主库、元数据库的打开数；cgroup 用量、上限与 OOM 计数；逐条 WebUI 连接；关键文件与目录大小；主库轻量统计（页数、各表最大 id、新增消息数） |
| 数据库总量 `db` | 1 小时 | 消息总数、图片按类型计数、黑话证据体量、近 30 天消息量（小时聚合表加未聚合尾部的近似值） |

黑话证据体量在 SQLite 3.43 及以后是字节数；更早的 SQLite 没有 `octet_length`，记的是字符数（中文为主时明显小于字节数），
以 `meta` 里的 `sqlite` 版本为准。

每次开始采集时还会写一条 `meta`：平台、Python 与 SQLite 版本、宿主版本、插件配置（白名单只记条目数）。
Linux 上另有内核与 glibc 版本、cgroup、Worker 环境里与分配器有关的变量（`worker_env`，只取 `MALLOC_ARENA_MAX`、
`MALLOC_TRIM_THRESHOLD_`、`MALLOC_MMAP_THRESHOLD_`、`MALLOC_TOP_PAD_`、`MALLOC_MMAP_MAX_`、`GLIBC_TUNABLES`、`PYTHONMALLOC`
这几个键，其他变量不记）；macOS 上另有内核与系统版本。Windows 与 macOS 不读 Worker 的环境，`worker_env` 这个键不存在，
不代表这些变量没设置。

内存采样不经过 Worker：每轮先读进程外的数据，再做一次 RPC 探测，然后写盘。探测最多等 5 秒（快速间隔不足 10 秒时等半个间隔），
所以 Worker 主循环卡住时样本不会断，只是每条样本晚这么一会儿落盘。探测没有及时返回就记 `rpc_pending_s`
（这个请求已等待的秒数），请求不取消，返回后在后面的样本里记下真实耗时，趋势记录的 `rpc_ms_max` 也包含它；
单个请求最多等 10 分钟，超过后记 `rpc_error` 并重新探测。探测失败记 `rpc_error`，不算采样失败。

cgroup 按 `mountinfo` 找挂载点，兼容 Docker 的私有与共享 cgroup 命名空间。读不到内存用量时
（memory 控制器未启用，例如树莓派默认内核；或进程位于根 cgroup）记 `unavailable` 与原因。

被观测的文件：

- `logs/expression_review/review_logs.json`（已发布的 1.2.3 至 v1.3.18 都是这个文件）；同目录的 `review_logs.jsonl`
  留给改成追加写的版本，已发布的版本里恒为 `null`
- `logs/memory_diagnostics/memory.jsonl`（v1.3.13 起）
- 统计报告 HTML（`MAIBOT_STATISTICS_REPORT_PATH`，默认 `maibot_statistics.html`）
- `data/local_store.json`
- 主库 `data/MaiBot.db` 与 A_Memorix 元数据库，以及它们的 `-wal`、`-shm`
- `logs/app_*.log.jsonl` 与 `logs/llm_cache_stats/` 的文件数与总大小

### 不会做的事

- 不读聊天内容、不读宿主配置文件原文、不记录完整环境变量与命令行。子进程按可执行文件名分类。
  Linux 上只查 Worker 及其直接子进程的命令行里有没有插件 Runner 的模块名，不取其中任何文本：
  npm 等会把含密钥的整条命令行改写进进程标题。Windows 与 macOS 不读命令行。
- 只以只读方式打开数据库（`mode=ro` 加 `query_only`），每条 SQL 有时限，超时记为测量结果而不是重试。
- 不写宿主的任何文件，只写自己的数据目录。
- 不把样本写进日志：宿主日志会推送到 WebUI。
- 不上传任何东西。诊断包由你决定是否发给别人。

## 输出文件

都在 `<实例根>/data/plugins/shinoyuki.memdiag/` 下，Docker（官方 compose）在 `docker-compose.yml` 所在目录下的
`data/MaiMBot-plugin-data/shinoyuki.memdiag/`：

| 文件 | 内容 |
| --- | --- |
| `fast.jsonl`、`fast.N.jsonl` | 快速样本，每行一条，`"k": "fast"` |
| `trend.jsonl`、`trend.N.jsonl` | `meta`、`trend`、`db`、`error`、`stop` 记录，按 `k` 区分 |
| `packs/memdiag-YYYYMMDD-HHMMSSZ.zip` | 诊断包（UTC 时间命名）：`meta.json`（不带路径的摘要、插件配置、一次即时的数据库总量统计）、`samples/` 下全部样本文件、`host_memory_diagnostics/` 下宿主自带的内存诊断日志（有的话） |

单个样本文件写满 `max_file_mb`（默认 8 MiB）后轮转，编号越大越旧。默认保留量大约是：
快速样本两到三周，趋势记录数月，诊断包 3 个；总占用上限约 110 MiB 加诊断包。调小保留个数后，
超出的旧文件在下次开始采集时删除，删不掉的（Windows 上正被别的程序打开）记一条警告，下次再删。
每写一行都 fsync，Worker 被 OOM 杀掉前最后的样本不会丢在缓冲里；磁盘写满或断电留下的半行会被跳过，
下一条记录另起一行。

Windows 上样本文件正被别的程序打开（例如正在打包，或 `Get-Content -Wait` 首次读完整个文件、输出被暂停时；
它空闲等待时不占用文件）时，轮转会失败：这一条记录（开始采集时写的 `meta` 也一样）写不进去、记一次采样失败，
下一轮从上次停下的地方接着轮转，不会多丢备份；一直打不开则按连续失败上限停止采集。

打包失败或被停止时会删掉写了一半的 `.partial`；宿主关停超时强杀 Runner 时来不及删，
留下的残片在之后打包时清理（超过 1 小时的）。旧包被别的程序占着删不掉时，新包照常生成，回复里会注明。

每条 `trend` 与 `db` 记录带 `wid`（Worker 的 pid 与启动时间）。Worker 重启后插件会停止采集并写一条
`stop` 记录；宿主重新拉起插件后以新的 `wid` 继续，分析时按 `wid` 分段。

Worker 连同插件一起被杀时来不及写 `stop`：Docker 里 OOM 会让整个容器重启，样本里表现为 `wid` 变了、前面没有 `stop`，
而且 pid 常常不变，要比启动时间。最后一条样本在被杀前的一个采样间隔内。

## 怎么看

主指标是 Linux 的匿名内存（`RssAnon`）、Windows 的提交内存（private usage）、macOS 的物理占用（`phys_footprint`）。
它比 RSS 更接近“程序真正占着不放的内存”，不受页缓存与共享库影响。三个平台的量不同，不要跨平台比数值，只看走势。

- 主指标按天近似线性上涨、不回落：Worker 里有东西在持续累积。对照同期的消息量、文件大小、
  连接数，看它和哪个量同步。
- 锯齿形、但每个低点都比上一个高：多半是缓存或碎片，峰值由某类周期任务决定。看 `trend` 里窗口的
  最小值走势比看末值更准。
- 某个时刻台阶式上跳并保持：对照那一刻的 `webui`、`error` 记录和宿主日志。
- `webui.stalled` 大于 0 且 `tx_max` 持续在高位：有 WebUI 客户端连着但不再读取（浏览器标签挂起、
  网络中断后连接未断开），服务端会为它缓存待发数据。
- `heap.heaps` 很多、`reserved_kb` 远大于 `committed_kb`：glibc 为很多线程各开了 arena；
  `meta.worker_env` 里可以看到是否设置了 `MALLOC_ARENA_MAX`。
- 被观测的文件持续变大：宿主若会整份读写这个文件，每次读写的内存峰值会随文件大小一起涨。
- `rpc_ms` 经常上百毫秒，或频繁出现 `rpc_pending_s`：Worker 主事件循环被阻塞，常与大文件读写、
  大查询同时出现。Worker 卡住超过 10 分钟时要把 `rpc_pending_s` 与 `E_TIMEOUT` 的 `rpc_error` 连起来看。
- `cgroup.events.oom_kill` 增加：容器或服务的内存上限触发过 OOM。容器重启后 cgroup 是新的，计数归零，
  所以没增加不代表没发生过：还要看 `wid` 有没有变、`RestartCount` 有没有涨。

摘要里的“近 N 小时增长”取的是不晚于 N 小时前的最后一个点。插件重载或 Runner 重启后，
历史从趋势文件里同一个 Worker 的记录恢复（刚轮转过时会多读一个旧文件）。中间停过采集（插件停用后重新启用）时，
实际跨度会明显长于 N 小时，摘要会写出实际跨度；最新样本过旧时也会注明采样时间。

## 配置

`config.toml` 由宿主根据插件声明生成，可在 WebUI 的插件配置页修改，修改后采集会按新配置重启。
重启前要先向宿主读几项配置；读不到时（例如 Worker 主循环卡住、RPC 超时）原来的采集按旧配置照常运行，
错误记在日志里，`/memdiag` 的摘要也会注明未按新配置重启，下次配置变更或重载时再试。
`[command].allowed_users` 不依赖采集重启，保存后立即生效。

| 段 | 键 | 默认 | 说明 |
| --- | --- | --- | --- |
| `[collector]` | `fast_interval_sec` | 15 | 5 至 300 |
| | `trend_interval_sec` | 300 | 60 至 3600 |
| | `db_interval_sec` | 3600 | 600 至 86400，会扫整表 |
| | `sql_limit_ms` | 300 | 单条只读 SQL 时限 |
| | `max_consecutive_failures` | 5 | 同一档连续失败多少次后停止采集 |
| `[storage]` | `max_file_mb` | 8 | 单个样本文件上限 |
| | `fast_backups` | 4 | 快速样本旧文件个数 |
| | `trend_backups` | 8 | 趋势记录旧文件个数 |
| | `keep_packs` | 3 | 诊断包保留个数 |
| `[command]` | `allowed_users` | `[]` | 见“命令权限” |

## 开发

```bash
python -m pytest
```

`tests/test_plugin.py` 需要安装 `maibot-plugin-sdk`，没有时自动跳过；其余测试只依赖标准库与 pytest。
平台专属的测试在别的平台上跳过；macOS 上装了 Command Line Tools 时，`tests/test_darwinproc.py` 会对照系统头文件校验结构体布局。
CI 在 Linux、Windows 与 macOS（arm64、Intel）上跑。`tests/host_smoke.sh` 用官方镜像确认插件能加载、写出真实样本、
数据库统计能跑通、Worker 在监听 WebUI 端口，推送到 `main` 时和每周各跑一次，宿主改配置键、表结构或进程结构时靠它发现。
公开仓库连续 60 天没有活动时 GitHub 会停用定时任务，用 `gh workflow enable host-smoke.yml` 恢复。

## 许可证

GPL-3.0-or-later，见 [LICENSE](LICENSE)。
