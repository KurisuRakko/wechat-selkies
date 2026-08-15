# WeChat Selkies

[![GitHub Stars](https://img.shields.io/github/stars/nickrunning/wechat-selkies?style=flat-square&logo=github&color=yellow)](https://github.com/nickrunning/wechat-selkies/stargazers)
[![GitHub Forks](https://img.shields.io/github/forks/nickrunning/wechat-selkies?style=flat-square&logo=github&color=blue)](https://github.com/nickrunning/wechat-selkies/network/members)
[![GitHub Issues](https://img.shields.io/github/issues/nickrunning/wechat-selkies?style=flat-square&logo=github&color=red)](https://github.com/nickrunning/wechat-selkies/issues)
[![GitHub License](https://img.shields.io/github/license/nickrunning/wechat-selkies?style=flat-square&color=green)](https://github.com/nickrunning/wechat-selkies/blob/master/LICENSE)
[![Docker Pulls](https://img.shields.io/docker/pulls/nickrunning/wechat-selkies?style=flat-square&logo=docker&color=blue)](https://hub.docker.com/r/nickrunning/wechat-selkies)
[![Docker Image Size](https://img.shields.io/docker/image-size/nickrunning/wechat-selkies?style=flat-square&logo=docker&color=orange)](https://hub.docker.com/r/nickrunning/wechat-selkies)
[![GitHub Release](https://img.shields.io/github/v/release/nickrunning/wechat-selkies?style=flat-square&logo=github&include_prereleases)](https://github.com/nickrunning/wechat-selkies/releases)
[![GitHub Workflow Status](https://img.shields.io/github/actions/workflow/status/nickrunning/wechat-selkies/docker.yml?style=flat-square&logo=github-actions&label=build)](https://github.com/nickrunning/wechat-selkies/actions)
[![GitHub Last Commit](https://img.shields.io/github/last-commit/nickrunning/wechat-selkies?style=flat-square&logo=github&color=purple)](https://github.com/nickrunning/wechat-selkies/commits)

中文 | [English](README_en.md)

基于 Docker 的微信/QQ Linux 客户端，使用 Selkies WebRTC 技术提供浏览器访问支持。

## 项目简介

本项目将官方微信/QQ Linux 客户端封装在 Docker 容器中，通过 Selkies 技术实现在浏览器中直接使用微信/QQ，无需在本地安装微信/QQ 客户端。适用于服务器部署、远程办公等场景。

## 升级注意事项

> 如果升级后部分功能缺失，请先清空本地挂载目录下的openbox目录(如`./config/.config/openbox`)。

> 仓库内置了上游微信版本自动检测机制：GitHub Actions 会定时检查官方 `.deb` 包版本，检测到变化后自动更新 `versions/upstream.env` 并触发镜像构建。

## 功能特性

- 🌐 **浏览器访问**：通过 Web 浏览器直接使用微信，无需本地安装
- 🐳 **Docker化部署**：简单的容器化部署，环境隔离
- 🔒 **数据持久化**：支持配置和聊天记录持久化存储
- 🎨 **中文支持**：完整的中文字体和本地化支持，支持本地中文输入法
- 🖼️ **图片复制**：支持通过侧边栏面板开启图片复制
- 📁 **文件传输**：支持通过侧边栏面板进行文件传输，侧边栏「文件」面板是一个可视化文件浏览器（见 [文件面板](#文件面板)）
- ⤓ **拖拽导出下载**：从微信聊天里把图片/文件往右上角一拖，直接下载到本机（见 [拖拽导出下载](#拖拽导出下载)）
- 🖥️ **AMD64和ARM64架构支持**：兼容主流CPU架构
- 🔧 **硬件加速**：可选的 GPU 硬件加速支持
- 🪟 **窗口切换器**：左上角增加切换悬浮窗，方便切换到后台窗口，为后续添加其它功能做基础
- 🤖 **自动启动**：可配置自动启动微信和QQ客户端（可选）
- 📋 **桌面快捷方式集成**：自动扫描 `~/Desktop/` 下的 `.desktop` 文件并添加到右键菜单，方便启动第三方应用（如通过 proot-apps 安装的应用）
- 📂 **文件管理器**：内置 PCManFM 轻量文件管理器，右键菜单即可启动，方便管理容器内文件
- 🎤 **麦克风转发**：浏览器麦克风可作为容器内微信的输入设备（Selkies 原生能力），支持语音留言与通话拾音

### 已锁定设置

dashboard 每次加载都会强制写入以下设置，并把对应设置项的整行 UI 彻底隐藏，避免浏览器旧的 `localStorage` 值在重连时重新生效：

| 设置 | 锁定值 |
|------|--------|
| HiDPI（像素完美） | 开 |
| 强制对齐分辨率 | 开 |
| 抗锯齿 | 开 |
| 使用 CSS 光标 | 开 |
| 界面缩放 | 200% |
| 编码器 | `x264enc-striped` |
| Encoder Rate Control Mode | CBR |
| 使用静态区域优化 | 开 |
| Turbo | 关 |
| CPU 编码 | 关 |

侧边栏的 **应用程序**、**共享** 和 **屏幕设置** 三块整卡面板会被隐藏，player 页面的浮动手柄按钮也会隐藏。屏幕设置只是隐藏 UI，锁定值仍会在每次加载时写入并生效。视频比特率滑块保持“越大越好”的自然方向；静态区域优化底层的 CRF 滑块使用 RTL 方向，画质最好的 `min` 在右侧、画质最差的 `max` 在左侧，滑块旁边的数字仍显示真实 CRF。界面缩放使用 Selkies 的 `scaling_dpi=192`（200%），服务端会自行通过 `xrdb` 应用 `Xft.dpi`，因此无需再改容器启动脚本。

### 画质预设

顶部栏提供四档画质，全部使用 `x264enc-striped` + CBR 码率控制；静态 CRF 数值越小，静态画面补发质量越高：

| 档位 | 帧率 | 码率 | 静态 CRF |
|------|------|------|----------|
| 省流 | 12 fps | 2 Mbps | 33 |
| 流畅（默认） | 24 fps | 6 Mbps | 28 |
| 高清 | 30 fps | 12 Mbps | 23 |
| 极致 | 60 fps | 20 Mbps | 18 |

每次页面加载会自动测速定档：测速使用容器内生成的 1MiB 同源文件，实测下行 `< 3 Mbps` 选省流，`3–8 Mbps` 选流畅，`8–15 Mbps` 选高清，`≥ 15 Mbps` 选极致；RTT 超过 150ms 时最多选流畅。测速 3 秒超时或失败会保持当前档位。用户手动点击任一档位后，本次会话不再自动改档，刷新后重新测速定档。

### 空闲自动省流

顶部栏新增一个可关闭的开关：页面切到后台、失去焦点，或超过 60 秒没有任何鼠标/键盘操作后，自动把画质临时降到与「省流」档位相同的设置（12 fps、2 Mbps、静态 CRF 33）；恢复可见或任意输入后立即还原为进入空闲前的实际设置，不会覆盖你手动选择的画质档位。默认开启，点按开关即可关闭；关闭时若正处于省电状态会立即还原。

### 文件面板

侧边栏「文件」面板是下载目录（默认 `~/Desktop`）的可视化文件浏览器：面包屑
（第一段固定「桌面」）+ 上级/刷新/筛选，可排序的四列表格（名称/修改日期/
类型/大小，目录恒在文件之前），双击目录进入、双击文件下载、把文件行拖出到
宿主机直接下载。以 `.` 开头的隐藏项（含 `.wechat-open-urls` 这类内部队列
文件）默认不显示。

旧版面板整块空白的根因：nginx 的 `/files` 块没有 `index` 指令，内建默认
`index index.html` 生效，下载目录里只要有一个用户存的 `index.html`，`/files/`
的清单请求就会返回那个 HTML 而不是目录清单，再叠上同一块的
`Content-Disposition: attachment`，附件不会在 iframe 里渲染。现在构建期把
该块改成 nginx autoindex 的 JSON 清单（`index` 指向一个不可能存在的名字
`.selkies-no-index`），面板由注入的 `wechat-file-manager.js` 直接 fetch
JSON 自绘，不再依赖 fancyindex 的 HTML 页面。

> 当 `SELKIES_FILE_TRANSFERS` 不含 `download` 或 `HARDEN_DESKTOP=true` 时，
> 上游 init-nginx 会把整个 `/files` 块删掉，面板会提示读取失败——这是上游
> 加固开关的预期行为。

### 麦克风转发

侧边栏顶部的麦克风图标是 Selkies 原生功能，本项目未做改造。使用步骤：先点一次侧边栏的麦克风图标并允许浏览器的麦克风权限，然后打开微信「设置 → 通话」，在麦克风下拉列表里选择 `SelkiesVirtualMic`。顺序不能反——虚拟设备是浏览器发出第一帧音频时才创建的，先开微信设置会看不到它。环境变量 `SELKIES_MICROPHONE_ENABLED`（默认 `true`）可关闭。注意：这只保证微信能读到麦克风音频；语音/视频通话能否接通取决于微信自身的网络穿透，与本项目无关。语音留言录制不依赖对端穿透，应当稳定可用。

### 拖拽导出下载

在微信聊天里**按住一张图片或一个文件开始往外拖**，右上角的画质预设条会暂时消失，
原位置变成一块绿色虚线的「拖到这里下载」投放区；把文件拖进去松手，浏览器就会像
普通链接一样把它下载下来，画质条随即恢复。中途在别处松手则什么也不会下载，画质
条同样恢复。一次拖多个文件会逐个触发下载。

之所以要这么绕，是因为整段拖拽自始至终都发生在远端 X11 会话里——浏览器只负责把
鼠标事件转发过去，页面上的 `dragover` / `drop` 永远不会触发。真正接住文件的是
`/scripts/wechat/wechat-export-drop.py`（s6 服务 `svc-wechat-export`）在拖拽期间
临时映射到远端屏幕右上角的一个 XDND 接收窗口；它建窗时不带背景位图，所以能收拖
拽却不会在画面上多出任何像素，页面上的投放区只是按它上报的矩形同步画出来的提示。
拖拽结束窗口立刻销毁，平时不存在，不会挡住微信自己的界面或任何点击。

接住的文件会复制到容器内的 `/config/.host-export/`（刻意不复用 `/config/Desktop`，
以免和拖入上传的文件混在一起形成回环），同名自动加序号，并且只保留**最近 20 个**，
旧的自动删除。下载链接是一次性 token，导出目录本身不对外暴露。

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `WECHAT_EXPORT_DIR` | `/config/.host-export` | 导出目录 |
| `WECHAT_EXPORT_KEEP` | `20` | 导出目录保留的文件数 |
| `WECHAT_EXPORT_PORT` | `8766` | 助手的 loopback API 端口（nginx 反代到 `/wechat-export/`） |

该助手必须以 root 运行：微信把聊天附件放在 `/config/xwechat_files/<账户>/` 下，
权限是 `0700 root`，桌面会话的 `abc` 用户读不到刚拖出来的那个文件。

> 只读的 `#shared` / `#player` 页面不启用此功能。若 10 秒内没收到"拖拽结束"信号
> （例如事件流断开），页面会自动把画质条放回去，不会把它永久藏掉。

## 截图展示
![微信截图](./docs/images/wechat-selkies-1.jpg)
![QQ截图](./docs/images/wechat-selkies-2.jpg)

## 快速开始

### 环境要求

- Docker
- Docker Compose
- 支持WebRTC的现代浏览器（Chrome、Firefox、Safari等）

### 快速部署

1. **直接使用已构建的镜像进行快速部署**

GitHub Container Registry镜像：
```bash
docker run -it -p 3001:3001 -v ./config:/config --device /dev/dri:/dev/dri ghcr.io/nickrunning/wechat-selkies:latest
```

Docker Hub镜像：
```bash
docker run -it -p 3001:3001 -v ./config:/config --device /dev/dri:/dev/dri nickrunning/wechat-selkies:latest
```

> **精简版镜像**：如果只需要微信（不含 QQ 和文件管理器），可使用 `minimal` 标签，镜像体积更小：
> ```bash
> docker run -it -p 3001:3001 -v ./config:/config --device /dev/dri:/dev/dri ghcr.io/nickrunning/wechat-selkies:minimal
> ```
> 精简版也支持版本号标签，如 `:1.2.3-minimal`、`:1.2-minimal`，方便锁定特定版本。

2. **访问微信**
   
   在浏览器中访问：`https://localhost:3001` 或 `https://<服务器IP>:3001`
   > **注意：** 映射3000端口用于HTTP访问，3001端口用于HTTPS访问，建议使用HTTPS。

### Docker Compose 部署
1. **创建项目目录并进入**
   ```bash
   mkdir wechat-selkies
   cd wechat-selkies
   ```
2. **创建 docker-compose.yml 文件**
   ```yaml
    services:
      wechat-selkies:
        image: nickrunning/wechat-selkies:latest    # or ghcr.io/nickrunning/wechat-selkies:latest
        container_name: wechat-selkies
        ports:
          - "${HTTP_PORT:-3000}:3000"
          - "${HTTPS_PORT:-3001}:3001"
        restart: unless-stopped
        volumes:
          - ./config:/config
        devices:
          - /dev/dri:/dev/dri
        environment:
          - PUID=${PUID:-1000}
          - PGID=${PGID:-100}
          - TZ=Asia/Shanghai
          - LC_ALL=zh_CN.UTF-8
          - AUTO_START_WECHAT=true
          - AUTO_START_QQ=false
          - CUSTOM_USER=${CUSTOM_USER:-}
          - PASSWORD=${PASSWORD:-}
        shm_size: "${SHM_SIZE:-1gb}"
    ```
3. **创建 `.env` 文件（可选）**

   复制 `.env.example` 并按需修改，未设置的变量将使用默认值：
   ```bash
   cp .env.example .env
   ```
   `.env` 文件示例：
   ```env
   HTTP_PORT=3000
   HTTPS_PORT=3001
   PUID=1000
   PGID=100
   # CUSTOM_USER=
   # PASSWORD=
   SHM_SIZE=1gb
   ```
4. **启动服务**
   ```bash
   docker compose up -d
   ```

### 源码部署

1. **克隆项目**
   ```bash
   git clone https://github.com/nickrunning/wechat-selkies.git
   cd wechat-selkies
   ```

2. **启动服务**
   ```bash
   docker compose up -d
   ```

3. **访问微信**

   在浏览器中访问：`https://localhost:3001` 或 `https://<服务器IP>:3001`

> **构建精简版**：源码部署时可通过 build-arg 构建仅含微信的精简镜像：
> ```bash
> docker build --build-arg INSTALL_QQ=false --build-arg INSTALL_PCMANFM=false -t wechat-selkies:minimal .
> ```

### 配置说明

更多自定义配置请参考 [Selkies Base Images from LinuxServer](https://github.com/linuxserver/docker-baseimage-selkies)。

#### Docker Hub 推送配置

本项目支持同时推送到 GitHub Container Registry 和 Docker Hub。如需启用 Docker Hub 推送功能，请在仓库下添加Environment Secrets和Environment Variables:

**Environment Secrets:**
* DOCKERHUB_USERNAME: 你的 Docker Hub 用户名
* DOCKERHUB_TOKEN: 你的 Docker Hub Access Token
**Environment Variables:**
* ENABLE_DOCKERHUB: 设置为 `true` 来启用 Docker Hub 推送

#### 环境变量配置

在 `docker-compose.yml` 中可以配置以下环境变量，支持通过 `.env` 文件覆盖带有 `${VAR:-default}` 的配置项：

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `TITLE` | `WeChat Selkies` | Web UI 标题 |
| `PUID` | `1000` | 用户 ID |
| `PGID` | `100` | 组 ID |
| `TZ` | `Asia/Shanghai` | 时区设置 |
| `LC_ALL` | `zh_CN.UTF-8` | 语言环境 |
| `CUSTOM_USER` | - | 自定义用户名（推荐设置） |
| `PASSWORD` | - | Web UI 访问密码（推荐设置） |
| `AUTO_START_WECHAT` | `true` | 是否自动启动微信客户端 |
| `AUTO_START_QQ` | `false` | 是否自动启动 QQ 客户端 |
| `ENABLE_WECHAT_NIGHTLY_RESTART` | `false` | 是否启用凌晨定时停止与自动重启微信 |
| `WECHAT_NIGHTLY_STOP_TIME` | `23:30` | 每日自动关闭微信的时刻（HH:MM 格式） |
| `WECHAT_NIGHTLY_START_TIME` | `01:30` | 每日自动重新启动微信的时刻（HH:MM 格式） |
| `ENABLE_WECHAT_AUTO_LOGIN` | `true` | 是否在微信启动/重启后自动识别并点击登录按钮 |
| `AUTO_LOGIN_DELAY` | `3` | 微信启动后等待 UI 渲染完成的秒数 |
| `ENABLE_WECHAT_WINDOW_WATCHDOG` | `true` | 是否常驻守护微信窗口（掉线自愈、托盘恢复、进程重拉都依赖它） |
| `WECHAT_FORCE_MAXIMIZED` | `true` | 是否强制保持微信主窗口最大化 |
| `WECHAT_WINDOW_CHECK_INTERVAL` | `5` | 窗口守护的巡检间隔（秒，最小 2） |
| `ENABLE_WECHAT_AUTO_RELOGIN` | `true` | 被服务端强制登出后是否自动点掉提示弹窗并重新登录（需窗口守护开启） |
| `WECHAT_RELOGIN_MAX_ATTEMPTS` | `3` | 单次登出事件内的最大重试次数，用尽后停止点击并在日志中说明 |
| `WECHAT_RELOGIN_RETRY_DELAY` | `30` | 两次重试之间的最小间隔（秒） |

> **窗口守护排障提示**：守护日志在 `/config/.wechat-watchdog.log`。想在容器里只查看将要执行的动作而不实际点击，可运行 `WECHAT_RELOGIN_DRY_RUN=true /scripts/wechat/wechat-window-watchdog.sh --relogin-once`，它会打印将要执行的命令但不会真正点击。



#### 端口配置

- `3001`: Web UI 访问端口

#### 数据卷挂载

- `./config:/config`: 微信配置和数据持久化目录

> **注意：** 如果升级后右键菜单缺少 `WeChat` 相关选项，请先清空本地挂载目录下的openbox目录(如`./config/.config/openbox`)。

### 可选：本机聊天记录 MCP

仓库包含一个默认关闭、仅用于本人本机非商用测试的聊天记录 MCP 集成。它通过
`INSTALL_WECHAT_HISTORY=true` 显式启用，只读取配置中固定的单一账户；回复工具只会
填写草稿，不提供自动发送。密钥扫描权限被隔离在一次性 Compose profile 中，主微信
容器不会获得 `SYS_PTRACE`。

完整的构建、密钥扫描、MCP 配置、安全边界和测试说明见
[本机微信聊天记录 MCP](docs/wechat-history.md)。请勿发布启用了该功能的公共镜像。

### 可选：关系洞察看板

在上面那套只读能力之上，还有一个**独立容器** `wechat-insights`：夜间离线统计私聊
记录，算出每个联系人的五维关系指标，用一个 Material Design 2 网页展示。它不改动主
微信容器的任何行为，只读挂载同一份数据，`metrics.db` 里只有统计数字、没有聊天内容。
构建、compose 合并方法、环境变量与安全注意事项见
[integrations/wechat-insights/README.md](integrations/wechat-insights/README.md)。
看板展示的是高度隐私的数据，端口只发布到 `127.0.0.1`，不要暴露公网。

### 可选：给两个站点绑域名

想用 `wechat.<你的域名>` / `relationship.<你的域名>` 代替「IP 加端口」访问，可以再加一个
独立的 Caddy 容器按 `Host` 分流。域名只解析到 Tailscale 地址、443 只绑定在回环与
Tailscale 地址上，所以「不挂 Tailscale 就访问不了」这条约束仍由网络层保证。做法、
证书（内置本地 CA）与验收清单见
[integrations/wechat-proxy/README.md](integrations/wechat-proxy/README.md)。

### 可选：摄像头转发（实验性，仅 Linux 宿主）

默认关闭。把浏览器的摄像头画面桥接成微信视频通话可选的虚拟摄像头（需要宿主
已加载 v4l2loopback 并把 `/dev/videoN` 传入容器）。**Windows + Docker
Desktop（WSL2 backend）宿主不支持**——原因和启用方法见
[docs/webcam-forwarding.md](docs/webcam-forwarding.md)。

## 安装第三方应用（如 Telegram）

本项目支持通过 [proot-apps](https://github.com/linuxserver/proot-apps) 安装第三方 Linux 应用。以 Telegram 为例：

1. 在浏览器中打开容器桌面
2. 点击左侧 **侧边栏** → **应用程序**（Applications）
3. 在应用列表中找到 **Telegram**
4. 点击 **安装**（Install）按钮，等待安装完成

安装完成后，应用快捷方式会自动出现在 `~/Desktop/` 目录下，**右键菜单会自动刷新**，无需重启容器即可从菜单中启动该应用。

> **提示：** 如需卸载应用，同样通过侧边栏 → 应用程序，选中对应应用后点击 **卸载**（Uninstall）即可，右键菜单会自动更新。

## 高级配置

### 硬件加速

如果您的系统支持 GPU 硬件加速，Docker Compose 配置中已包含相关设备映射：

```yaml
devices:
  - /dev/dri:/dev/dri
```

## 目录结构

```
wechat-selkies/
├── docker-compose.yml          # Docker Compose 配置文件
├── .env.example                # 环境变量示例文件
├── Dockerfile                  # Docker 镜像构建文件
├── LICENSE                     # License
├── README.md                   # 项目说明文档
├── config/                     # 配置和数据持久化目录
└── root/                       # 容器初始化文件
    ├── defaults/
    │   └── autostart           # 自动启动配置
    └── wechat.png              # 微信图标
```

## 故障排除

### 更新微信/QQ版本

当微信或QQ提示"版本过期"时，只需重新拉取最新镜像并重建容器即可，聊天记录和配置不受影响：

```bash
# 使用预构建镜像
docker compose pull && docker compose up -d

# 使用源码构建
git pull && docker compose up -d --build
```

> **注意：** 微信和QQ的安装包 URL 指向官方最新版本，重新构建镜像时会自动下载最新版。

对于仓库维护者，当前自动化流程如下：

1. `Detect Upstream Package Updates` 每 6 小时检查一次微信官方安装包版本，也支持手动触发
2. 如果检测到版本号或安装包哈希变化，工作流会更新 `versions/upstream.env`
3. 该文件变更提交到 `master` 后，会自动触发 `Build and Publish Docker Image`

版本状态文件位于 `versions/upstream.env`，当前记录了：

- 微信 amd64/arm64 下载地址
- 微信 amd64/arm64 解析出的版本号
- 微信 amd64/arm64 安装包 SHA256
- 最近一次发生变更的检测时间

### 常见问题

1. **无法访问 Web UI**
   - 检查端口 3001 是否被占用
   - 确认 Docker 容器正常运行：`docker ps`

### 日志查看

查看容器运行日志：
```bash
docker compose logs -f wechat-selkies
```

## 技术架构

- **基础镜像**：`ghcr.io/linuxserver/baseimage-selkies:ubuntunoble`
- **微信客户端**：官方微信 Linux 版本
- **Web 技术**：Selkies WebRTC
- **容器化**：Docker + Docker Compose

## 贡献指南

欢迎提交 Issue 和 Pull Request！

1. Fork 本项目
2. 创建特性分支：`git checkout -b feature/your-feature`
3. 提交更改：`git commit -am 'Add some feature'`
4. 推送分支：`git push origin feature/your-feature`
5. 提交 Pull Request

## 许可证

本项目采用 **MIT License** 开源协议。详见 [LICENSE](LICENSE) 文件。

### 📜 许可证说明

- **项目许可证**: MIT License - 宽松的开源许可证
- **依赖项说明**: 本项目使用 [LinuxServer.io baseimage-selkies](https://github.com/linuxserver/docker-baseimage-selkies) 作为基础镜像
- **许可证兼容性**: 由于本项目仅使用基础镜像而未修改其源码，根据容器化软件的许可证实践，可以采用MIT许可证
- **源码开放**: 完整项目源代码在 GitHub 上公开：https://github.com/nickrunning/wechat-selkies

## 免责声明与版权声明

### 🚨 重要声明

**本项目与腾讯公司无任何关联，属于独立的第三方开源项目。**

### 📋 版权声明

- **微信®** 是 **腾讯公司** 的注册商标和版权作品
- 本项目中使用的微信相关图标、logo 等视觉元素的版权归腾讯公司所有
- 本项目仅为技术展示和学习目的，不用于商业用途
- **如有版权争议，将立即移除相关内容**

### ⚖️ 法律合规

- 本项目严格遵守相关法律法规和用户协议
- 用户使用本项目时应遵守当地法律法规
- 本项目不对用户的使用行为承担法律责任
- **如腾讯公司认为存在侵权行为，请联系我们立即处理**

### 🎯 使用条款

- 本项目仅供学习、研究和个人使用
- 禁止用于任何商业目的或盈利活动
- 用户应自行承担使用风险和法律责任
- 请遵守微信用户协议和相关服务条款

## 相关链接

- [微信官方网站](https://weixin.qq.com/)
- [Selkies WebRTC](https://github.com/selkies-project)
- [LinuxServer.io](https://github.com/linuxserver)
- [xiaoheiCat/docker-wechat-sogou-pinyin](https://github.com/xiaoheiCat/docker-wechat-sogou-pinyin)

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=nickrunning/wechat-selkies&type=Date)](https://www.star-history.com/#nickrunning/wechat-selkies&Date)
