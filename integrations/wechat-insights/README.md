# 关系洞察看板（wechat-insights）

一个**独立运行**的容器：夜里离线分析微信私聊记录，算出每个联系人的五维「关系指标」，
再用一个 Material Design 2 风格的网页展示。它不改动主微信容器的任何行为，也不会向
微信写入任何东西。

v1 只做纯统计分析，不接任何 LLM；深度维度用词法代理，扩展点留在
[`wechat_insights/depth.py`](wechat_insights/depth.py)。

## 它做了什么

- 复用 [`wechat_history`](../wechat-history) 包完成解密、只读快照、账户白名单校验与
  私聊会话过滤——本目录里没有任何一行重新实现的解密或读库代码。
- 每天固定时刻（默认 04:30）跑一轮**增量**分析：按会话游标只读新消息，
  首轮做全量回填。
- 消息原文只在内存里过一遍词法统计（字数 / 是否疑问句 / 「哈」连击），随后立即丢弃。
  `metrics.db` 里**只有统计数字**，没有任何聊天内容。
- 结果预计算进 `scores` 表，页面打开是秒开的，HTTP 处理器不做全量扫描。

## 五维指标

| 维度 | 组成（权重） |
| --- | --- |
| 响应 Responsiveness | TA 回复我的延迟中位数（0.6，越快越高）、TA 的秒回率 <60s（0.4） |
| 主动 Initiative | TA 发起对话占比（0.4）、TA 的追加率（0.3）、TA 说最后一句的占比（0.3） |
| 投入 Investment | 类型加权成本日均（0.45）、消息量日均（0.2）、字数日均（0.2）、双方表情包互发频率（0.15） |
| 节奏 Rhythm | 深夜 23:00–02:00 占比（0.3）、周末占比（0.2）、平均对话轮次（0.3）、长对话（>20 轮）占比（0.2） |
| 深度 Depth | TA 消息平均长度（0.4）、疑问句占比（0.3）、长消息 >50 字占比（0.3） |

计算方式：

- **对话切分**：相邻消息间隔超过 6 小时即切开为两段对话。
- **回复延迟**：只在对话内部、方向翻转处计一次回复，延迟是翻转处两条消息的时间差；
  跨对话的间隔不算回复。
- **类型成本权重**：通话 8、语音 3、视频 3、图片 1.5、表情 1，文字按每 20 字 1 个单位。
- **归一化**：绝对分没有意义。每个组成项先在「近 90 天有 ≥50 条往来消息的所有联系人」
  这个群体里取百分位（0–100），再按权重加权成维度分；综合分是五维的平均。
  某个组成项缺样本时，它的权重按比例分给同维度的其余项。
- **趋势**：同一批联系人内，「近 30 天」窗口的维度分减去「其前 90 天」窗口的维度分，
  差值超过 8 分才显示升/降箭头。
- 近 90 天往来消息不足 50 条的联系人不打分，卡片显示「数据不足」。
- 「我方」的回复延迟与秒回率也会算出来用于对比展示，但不计入 TA 的分。

指标按**天**分桶存储，月度视图由天桶合成。（任务书原本写的是按自然月存储，但那样
「近 30 天 / 近 90 天」只能退化成整月窗口；按天存既满足滚动窗口的精确性，也能无损地
合成出月度图表，存储量对单机来说完全不是问题。）

## 构建与部署

```bash
docker build -f integrations/wechat-insights/Dockerfile -t wechat-insights:latest .
```

构建上下文必须是仓库根目录——镜像里同时需要 `wechat_history` 与 `wechat_insights`。
ECharts 在构建时从 npm registry 下载并校验 sha256，运行时不依赖任何 CDN。

部署时把 [`compose.example.yml`](compose.example.yml) 的内容合并进你自己的
`compose.local.yml`（该文件不在仓库里），逐项核对带 `⚠️ 需要替换` 注释的地方：

- 微信数据卷**只读**挂到 `/history-source`。默认是 named volume（与主容器 `/config`
  共用同一个卷），并在 `volumes:` 里声明为 `external`——卷不存在时 compose 直接报错，
  而不是静默建一个空卷让看板「一切正常但零条消息」。只有当你把微信数据目录复制到
  宿主机某处、想对着副本分析时，才改用注释里备选的 bind 挂载。两种方式下
  healthcheck 都会检查源目录里有没有任何 `*.db`，空目录判为不健康。
- keyscan 写密钥的那个 named volume **只读**挂到 `/run/wechat-history`
  （卷名照抄你现有 compose 里 `history-keyscan` profile 用的那个）
- tmpfs 挂到 `/run/wechat-history-cache`，明文快照只落在这里
  （完整参数 `rw,noexec,nosuid,nodev,size=1g,mode=0700,uid=…,gid=…`，每一项都有安全
  理由，见示例里的逐项注释；Docker 传任何选项都会整体替换 tmpfs 默认挂载参数，
  所以必须显式写全安全选项，否则明文缓存会以 exec/suid 挂载）
- 新的 named volume 挂 `/data`，存放 `metrics.db`

```bash
docker compose -f docker-compose.yml -f compose.local.yml up -d --build wechat-insights
```

密钥文件权限是 0600、属主是主容器里的 `abc` 用户，所以本容器的运行 uid/gid 必须与
主容器的 `PUID`/`PGID` 一致（默认 1000:100）——compose 示例里同时通过构建参数、
`user:` 和 tmpfs 的 `uid=`/`gid=` 三处指定，必须全部保持相同，否则读不到密钥、
也写不进缓存目录。`/data` 命名卷首次创建时会继承镜像里该目录的属主，因此非 root
进程也能写。

### 端口发布

看板展示的是**极其私密的聊天统计**，只允许通过回环地址或你自己的 Tailscale 地址
（100.x 网段）访问，示例里发布到 `127.0.0.1:8300` 与你的 Tailscale 地址。**不要**
发布成 `0.0.0.0:8300` 或 `192.168.x.x:8300`——局域网里任何人都能看到它，等于把
微信聊天记录挂在公网上。需要远程访问就走 Tailscale，不要开端口转发。

### 内存与 tmpfs 的取舍

tmpfs 的大小直接决定明文缓存的写满点：缓存数据量达到 `size=` 上限后，任何写入都会
硬失败（`CACHE_LIMIT`）。示例里的 `size=1g` 配合 `WECHAT_HISTORY_MAX_CACHE_BYTES`
设成 960 MiB，实测九个消息分片全量解密峰值 474.9 MiB（约 46%），留足了余量。上限
必须 ≤ tmpfs 的 `size=`，否则缓存写满前先撞上 tmpfs 的内存上限，拿到的是 ENOSPC 而
不是干净的 `CACHE_LIMIT`——两处要一起改。tmpfs 只在实际写入时才占用内存，不会启动
即占满 1g——但宿主机 Docker VM 总共只有 8 GiB，主微信容器已经用掉约 4.5 GiB，所以
这 1g 是真实预算，调大上限前先看看内存余量。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `INSIGHTS_ANALYZE_TIME` | `04:30` | 每天跑一轮分析的时刻（HH:MM，容器本地时区） |
| `INSIGHTS_AUTH_TOKEN` | 空 | 访问令牌，见下方「安全」。留空只打印一条启动警告 |
| `INSIGHTS_DB_PATH` | `/data/metrics.db` | 分析结果数据库 |
| `INSIGHTS_BIND_HOST` | `0.0.0.0` | 容器内监听地址 |
| `INSIGHTS_BIND_PORT` | `8300` | 容器内监听端口 |
| `INSIGHTS_RUN_ON_START` | `true` | 从未成功分析过时，启动后立即回填 |
| `INSIGHTS_SESSION_GAP_SECONDS` | `21600` | 对话切分阈值（6 小时） |
| `INSIGHTS_SCORE_WINDOW_DAYS` | `90` | 打分窗口 |
| `INSIGHTS_TREND_RECENT_DAYS` | `30` | 趋势的「近期」窗口 |
| `INSIGHTS_TREND_BASELINE_DAYS` | `90` | 趋势的基线窗口（紧邻近期窗口之前） |
| `INSIGHTS_MIN_SCORE_MESSAGES` | `50` | 打分窗口内的最低消息数，不够就是「数据不足」 |
| `INSIGHTS_BACKFILL_BATCH` | `5000` | 单批读取的消息条数 |
| `INSIGHTS_DEPTH_STRATEGY` | `lexical` | 深度维度策略，v1 只有词法策略 |
| `TZ` | 容器默认 | 决定「按天 / 深夜 / 周末」的切分，建议设成你自己的时区 |

身份三变量（`WECHAT_HISTORY_ACCOUNT_DIR`、`WECHAT_HISTORY_USERNAME`、
`WECHAT_HISTORY_IDENTITY_TOKENS`）必须设置，取值与主容器完全一致，见
[docs/wechat-history.md](../../docs/wechat-history.md)。明文缓存上限
`WECHAT_HISTORY_MAX_CACHE_BYTES`（默认 512 MiB）也是 `wechat_history` 的旋钮，
本容器要扫全部会话，需要按上一节调大。`wechat_history` 的路径变量
（`WECHAT_HISTORY_SOURCE_ROOT`、`WECHAT_HISTORY_KEYS_FILE`、
`WECHAT_HISTORY_CACHE_ROOT`）沿用默认值即可，compose 示例里的挂载点与它们一一对应。

## 安全注意事项

- **不要暴露公网或局域网。** 看板展示的是**极其私密的聊天统计**——谁在什么时候、
  以什么频率和你聊天、聊到多晚、沉默多久，这些都能从页面上看出来。端口只发布到
  `127.0.0.1:8300` 和你的 Tailscale 地址（100.x），需要远程访问就走 Tailscale；
  不要把端口发布到 `0.0.0.0` 或局域网地址，也不要放在没有鉴权的反向代理后面。
- **设置 `INSIGHTS_AUTH_TOKEN`。** 设置后所有请求（包括静态资源）都必须携带
  `Authorization: Bearer <token>`（API 客户端）或 `?token=<token>`（浏览器首次访问）。
  查询参数通过校验后，服务端会写一个 HttpOnly cookie 并 302 到不带参数的同一个
  URL——token 不会留在地址栏、浏览器历史或复制出去的链接里。用一段足够长的随机串：

  ```bash
  openssl rand -hex 32
  ```

- **源数据严格只读。** `/history-source` 与 `/run/wechat-history` 都以 `:ro` 挂载，
  容器永远不会写微信数据目录。解密出的明文只存在于 tmpfs（`noexec,nosuid,nodev`，
  且 `mode=0700`），容器停止即消失。
- **URL 里没有 wxid。** 详情页用 `sha256(session_id)` 的前 24 位做标识，
  API 响应里也不包含 session_id。
- 只分析私聊：群聊、公众号、系统会话、隐藏会话、不在通讯录里的会话全部排除，
  判断逻辑与推送服务共用
  [`wechat_history/sessions.py`](../wechat-history/wechat_history/sessions.py)。
- 与 `wechat-history` 相同的法律与伦理约束在这里同样适用，见
  [`docs/wechat-history.md`](../../docs/wechat-history.md)。

## API

全部只读 `metrics.db`（`POST /api/refresh` 除外）。

| 接口 | 说明 |
| --- | --- |
| `GET /api/status` | 上次分析时间、是否正在分析、下次计划时间、错误信息 |
| `GET /api/contacts` | 全部联系人的五维分、趋势、近 30 天消息数 |
| `GET /api/contact/<hash>` | 单个联系人的雷达、月度序列、类型构成、里程碑、近期异动 |
| `POST /api/refresh` | 手动触发一轮分析；正在分析时返回 409 |

## 测试

在容器里跑：

```bash
docker exec wechat-insights python3 -m unittest discover -s /opt/wechat-insights/tests -t /opt/wechat-insights -v
```

或者用 PowerShell 脚本：

```powershell
pwsh -NoProfile -File .\integrations\wechat-insights\scripts\test.ps1
```

核心纯逻辑（对话切分、回复延迟、发起/结束判定、类型加权、百分位归一化、按天聚合、
增量游标）都用构造的内存消息序列覆盖，不依赖真实数据库。
