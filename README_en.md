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

English | [中文](README.md)

Docker-based WeChat/QQ Linux client with browser access support using Selkies WebRTC technology.

## Project Overview

This project packages the official WeChat/QQ Linux client in a Docker container, enabling direct WeChat/QQ usage in browsers through Selkies technology without local installation. Suitable for server deployment, remote work, and other scenarios.

## Upgrade Notes

> If some features are missing after an upgrade, please clear the `openbox` directory in the local mounted directory (e.g., `./config/.config/openbox`).

> This repository includes automatic upstream WeChat version detection: GitHub Actions periodically checks the official `.deb` packages, updates `versions/upstream.env` when changes are detected, and then triggers the image build workflow.

## Features

- 🌐 **Browser Access**: Use WeChat directly through web browsers without local installation
- 🐳 **Dockerized Deployment**: Simple containerized deployment with environment isolation
- 🔒 **Data Persistence**: Supports persistent storage of configurations and chat records
- 🎨 **Chinese Support**: Complete Chinese fonts and localization support, including local Chinese input methods
- 🖼️ **Image Copy**: Support image copying through sidebar panel
- 📁 **File Transfer**: Support file transfer through sidebar panel
- ⤓ **Drag-out Export**: Drag an image or file out of a WeChat chat into the top-right corner to download it locally (see [Drag-out Export](#drag-out-export))
- 🖥️ **AMD64 and ARM64 Architecture Support**: Compatible with mainstream CPU architectures
- 🔧 **Hardware Acceleration**: Optional GPU hardware acceleration support
- 🪟 **Window Switcher**: Added a floating window switcher in the top left corner for easy switching to background windows, laying the foundation for adding other features in the future
- 🤖 **Auto Start**: Configurable auto-start for WeChat and QQ clients (optional)
- 📋 **Desktop Shortcut Integration**: Automatically scans `.desktop` files in `~/Desktop/` and adds them to the right-click menu, making it easy to launch third-party applications (e.g., apps installed via proot-apps)
- 📂 **File Manager**: Built-in PCManFM lightweight file manager, accessible from the right-click menu for easy file management inside the container

### Locked Settings

Every dashboard load re-applies the following settings and hides their entire setting rows, so stale browser `localStorage` values cannot override them after a reconnect:

| Setting | Locked value |
|---------|--------------|
| HiDPI (Pixel Perfect) | On |
| Force Aligned Resolution | On |
| Anti-aliasing | On |
| Use CSS Cursors | On |
| UI Scaling | 200% |
| Encoder | `x264enc-striped` |
| Encoder Rate Control Mode | CBR |
| Use Static Region Optimization | On |
| Turbo | Off |
| CPU Encoding | Off |

The **Applications**, **Sharing**, and **Screen Settings** sidebar cards are hidden entirely, and the floating gamepad button on player pages is hidden too. Screen Settings is UI-only hiding; the locked values are still written and applied on every load. The video bitrate slider keeps its natural higher-is-better direction; the static optimization CRF slider uses RTL direction, so the best-quality `min` is on the right and the worst-quality `max` is on the left, while the readout still shows the real CRF. UI scaling uses Selkies `scaling_dpi=192` (200%), and the server applies `Xft.dpi` through `xrdb` itself, so no container startup change is needed.

### Quality Presets

The top bar offers four quality presets, all using `x264enc-striped` with CBR rate control. Lower static CRF means higher quality for static scene repaints:

| Preset | Framerate | Bitrate | Static CRF |
|--------|-----------|---------|------------|
| 省流 | 12 fps | 2 Mbps | 33 |
| 流畅 (default) | 24 fps | 6 Mbps | 28 |
| 高清 | 30 fps | 12 Mbps | 23 |
| 极致 | 60 fps | 20 Mbps | 18 |

Every page load runs an automatic speed test against a generated 1 MiB same-origin file: measured download `< 3 Mbps` selects 省流, `3–8 Mbps` selects 流畅, `8–15 Mbps` selects 高清, and `>= 15 Mbps` selects 极致. If RTT exceeds 150ms, the automatic pick is capped at 流畅. A 3-second timeout or failure keeps the current preset. Once the user manually clicks any preset, auto selection is disabled for that session and returns after a page refresh.

### Drag-out Export

**Press and drag an image or file out of a WeChat chat** and the quality preset bar in
the top-right corner disappears, replaced in place by a green dashed "拖到这里下载"
(drop here to download) zone. Release the file over that zone and the browser downloads
it like any ordinary link, then the preset bar comes back. Releasing anywhere else
downloads nothing and restores the bar just the same. Dragging several files at once
triggers one download each.

The indirection is unavoidable: the whole drag happens inside the remote X11 session —
the browser only forwards pointer events — so `dragover` / `drop` never fire on the
page. What actually catches the file is an XDND window that
`/scripts/wechat/wechat-export-drop.py` (s6 service `svc-wechat-export`) maps over the
remote screen's top-right corner for exactly as long as a drag is running. It is created
with no background pixmap, so it receives the drop without changing a single pixel on
screen, and the page's drop zone is drawn from the rectangle the helper reports. The
window is destroyed as soon as the drag ends, so it never covers WeChat's own UI or
swallows a click.

Caught files are copied into `/config/.host-export/` inside the container (deliberately
not `/config/Desktop`, so drag-out exports cannot loop back through drag-in uploads),
numbered on name collisions, and pruned to the **20 most recent**. Download links are
one-shot tokens; the export directory itself is never exposed.

| Environment variable | Default | Purpose |
|----------------------|---------|---------|
| `WECHAT_EXPORT_DIR` | `/config/.host-export` | Export directory |
| `WECHAT_EXPORT_KEEP` | `20` | Files kept in the export directory |
| `WECHAT_EXPORT_PORT` | `8766` | Helper loopback API port (nginx proxies `/wechat-export/`) |

The helper has to run as root: WeChat stores chat attachments under
`/config/xwechat_files/<account>/` with mode `0700 root`, which the desktop `abc` user
cannot read.

> Read-only `#shared` / `#player` pages do not enable this. If no drag-end arrives within
> 10 seconds (a dropped event stream, say), the page restores the preset bar on its own
> rather than hiding it forever.

## Screenshots
![WeChat Screenshot](./docs/images/wechat-selkies-1.jpg)
![QQ Screenshot](./docs/images/wechat-selkies-2.jpg)

## Quick Start

### Requirements

- Docker
- Docker Compose
- Modern browser with WebRTC support (Chrome, Firefox, Safari, etc.)

### Quick Deployment

1. **Direct deployment using pre-built images**
GitHub Container Registry image:
```bash
docker run -it -p 3001:3001 -v ./config:/config --device /dev/dri:/dev/dri ghcr.io/nickrunning/wechat-selkies:latest
```
Docker Hub image:
```bash
docker run -it -p 3001:3001 -v ./config:/config --device /dev/dri:/dev/dri nickrunning/wechat-selkies:latest
```

> **Minimal image**: If you only need WeChat (without QQ and file manager), use the `minimal` tag for a smaller image:
> ```bash
> docker run -it -p 3001:3001 -v ./config:/config --device /dev/dri:/dev/dri ghcr.io/nickrunning/wechat-selkies:minimal
> ```
> Versioned minimal tags are also available, e.g. `:1.2.3-minimal`, `:1.2-minimal`, for pinning to a specific release.

2. **Access WeChat**
   
   Open in browser: `https://localhost:3001` or `https://<server-ip>:3001`
   > **Note**: 3001 port is for HTTPS access. If you need HTTP access, please map port 3000 as well.

### Docker Compose Deployment
1. **Create project directory and navigate into it**
   ```bash
   mkdir wechat-selkies
   cd wechat-selkies
   ```
2. **Create `docker-compose.yml` file with the following content**
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
3. **Create `.env` file (optional)**

   Copy `.env.example` and modify as needed. Variables not set will use default values:
   ```bash
   cp .env.example .env
   ```
   `.env` file example:
   ```env
   HTTP_PORT=3000
   HTTPS_PORT=3001
   PUID=1000
   PGID=100
   # CUSTOM_USER=
   # PASSWORD=
   SHM_SIZE=1gb
   ```
4. **Start the service**
   ```bash
   docker compose up -d
   ```

### Source Code Deployment

1. **Clone the repository**
   ```bash
   git clone https://github.com/nickrunning/wechat-selkies.git
   cd wechat-selkies
   ```

2. **Start the service**
   ```bash
   docker compose up -d
   ```

3. **Access WeChat**

   Open in browser: `https://localhost:3001` or `https://<server-ip>:3001`

> **Build minimal version**: When building from source, use build-arg to create a WeChat-only image:
> ```bash
> docker build --build-arg INSTALL_QQ=false --build-arg INSTALL_PCMANFM=false -t wechat-selkies:minimal .
> ```

### Configuration

For more custom configurations, please refer to [Selkies Base Images from LinuxServer](https://github.com/linuxserver/docker-baseimage-selkies).

#### Docker Hub Push Configuration
This project supports pushing to both GitHub Container Registry and Docker Hub. Docker Hub push is optional and requires manual configuration. Please add the following Environment Secrets and Environment Variables in your repository to enable Docker Hub push functionality:

**Environment Secrets:**
* `DOCKERHUB_USERNAME`: Your Docker Hub username
* `DOCKERHUB_TOKEN`: Your Docker Hub Access Token

**Environment Variables:**
* `ENABLE_DOCKERHUB`: Set to `true` to enable Docker Hub push

#### Environment Variables

Configure the following environment variables in `docker-compose.yml`. Variables with `${VAR:-default}` syntax can be overridden via a `.env` file:

| Variable | Default | Description |
|----------|---------|-------------|
| `TITLE` | `WeChat Selkies` | Web UI title |
| `PUID` | `1000` | User ID |
| `PGID` | `100` | Group ID |
| `TZ` | `Asia/Shanghai` | Timezone setting |
| `LC_ALL` | `zh_CN.UTF-8` | Locale setting |
| `CUSTOM_USER` | - | Custom username (recommended) |
| `PASSWORD` | - | Web UI access password (recommended) |
| `AUTO_START_WECHAT` | `true` | Whether to automatically start the WeChat client |
| `AUTO_START_QQ` | `false` | Whether to automatically start the QQ client |
| `ENABLE_WECHAT_NIGHTLY_RESTART` | `false` | Whether to enable nightly auto-stop and auto-restart for WeChat |
| `WECHAT_NIGHTLY_STOP_TIME` | `23:30` | Daily scheduled stop time for WeChat (HH:MM format) |
| `WECHAT_NIGHTLY_START_TIME` | `01:30` | Daily scheduled restart time for WeChat (HH:MM format) |
| `ENABLE_WECHAT_AUTO_LOGIN` | `true` | Whether to automatically detect and click the login button after WeChat starts/restarts |
| `AUTO_LOGIN_DELAY` | `3` | Delay in seconds after WeChat launch to wait for UI rendering before login action |



#### Port Configuration

- `3001`: Web UI access port

#### Volume Mounts

- `./config:/config`: WeChat configuration and data persistence directory

> **Note:** If the right-click menu lacks `WeChat` related options after an upgrade, please clear the `openbox` directory in the local mounted directory (e.g., `./config/.config/openbox`).

### Optional Local Chat History MCP

The repository includes a disabled-by-default, single-account chat-history MCP
integration for private, local, non-commercial testing. Enable it explicitly
with `INSTALL_WECHAT_HISTORY=true`. Its reply tool only prepares a draft in the
WeChat input box and never sends it; process-memory access is confined to a
one-shot Compose helper and is never granted to the main container.

See [Local WeChat Chat History MCP](docs/wechat-history.md) for setup, security
boundaries, testing, and legal cautions. Do not publish a public image with this
feature enabled.

## Installing Third-Party Applications (e.g., Telegram)

This project supports installing third-party Linux applications via [proot-apps](https://github.com/linuxserver/proot-apps). Here's how to install Telegram as an example:

1. Open the container desktop in your browser
2. Click the **Sidebar** on the left → **Applications**
3. Find **Telegram** in the application list
4. Click the **Install** button and wait for the installation to complete

Once installed, the application shortcut will automatically appear in the `~/Desktop/` directory, and the **right-click menu will auto-refresh** — no container restart needed to launch the app from the menu.

> **Tip:** To uninstall an application, go to Sidebar → Applications, select the app, and click **Uninstall**. The right-click menu will update automatically.

## Advanced Configuration

### Hardware Acceleration

If your system supports GPU hardware acceleration, the Docker Compose configuration includes relevant device mapping:

```yaml
devices:
  - /dev/dri:/dev/dri
```

## Directory Structure

```
wechat-selkies/
├── docker-compose.yml          # Docker Compose configuration file
├── .env.example                # Environment variables example file
├── Dockerfile                  # Docker image build file
├── LICENSE                     # License
├── README.md                   # Project documentation (Chinese)
├── README_en.md                # Project documentation (English)
├── config/                     # Configuration and data persistence directory
└── root/                       # Container initialization files
    ├── defaults/
    │   └── autostart           # Auto-start configuration
    └── wechat.png              # WeChat icon
```

## Troubleshooting

### Updating WeChat/QQ Version

When WeChat or QQ displays a "version outdated" message, simply pull the latest image and recreate the container. Your chat history and configurations will be preserved:

```bash
# Using pre-built images
docker compose pull && docker compose up -d

# Using source code build
git pull && docker compose up -d --build
```

> **Note:** The WeChat and QQ download URLs point to the latest official versions. Rebuilding the image will automatically download the newest version.

For maintainers, the current automation flow is:

1. `Detect Upstream Package Updates` checks the official WeChat packages every 6 hours and can also be triggered manually
2. If the version or package hash changes, the workflow updates `versions/upstream.env`
3. Once that file is committed to `master`, it automatically triggers `Build and Publish Docker Image`

The version state file is stored at `versions/upstream.env` and currently records:

- WeChat amd64/arm64 download URLs
- Parsed WeChat amd64/arm64 package versions
- WeChat amd64/arm64 package SHA256 hashes
- The last detection time that actually changed the tracked state

### Common Issues

1. **Unable to access Web UI**
   - Check if port 3001 is occupied
   - Confirm Docker container is running normally: `docker ps`

### Log Viewing

View container runtime logs:
```bash
docker compose logs -f wechat-selkies
```

## Technical Architecture

- **Base Image**: `ghcr.io/linuxserver/baseimage-selkies:ubuntunoble`
- **WeChat Client**: Official WeChat Linux version
- **Web Technology**: Selkies WebRTC
- **Containerization**: Docker + Docker Compose

## Contributing

Issues and Pull Requests are welcome!

1. Fork this project
2. Create feature branch: `git checkout -b feature/your-feature`
3. Commit changes: `git commit -am 'Add some feature'`
4. Push branch: `git push origin feature/your-feature`
5. Submit Pull Request

## License

This project is licensed under **MIT License**. See the [LICENSE](LICENSE) file for details.

### 📜 License Statement

- **Project License**: MIT License - A permissive open source license
- **Dependency Note**: This project uses [LinuxServer.io baseimage-selkies](https://github.com/linuxserver/docker-baseimage-selkies) as base image
- **License Compatibility**: Since this project only uses the base image without modifying its source code, following containerized software licensing practices, it can adopt the MIT license
- **Open Source**: Complete project source code is publicly available on GitHub: https://github.com/nickrunning/wechat-selkies

## Disclaimer and Copyright Notice

### 🚨 Important Statement

**This project has no affiliation with Tencent and is an independent third-party open source project.**

### 📋 Copyright Notice

- **WeChat®** is a registered trademark and copyrighted work of **Tencent**
- The copyright of WeChat-related icons, logos and other visual elements used in this project belongs to Tencent
- This project is for technical demonstration and learning purposes only, not for commercial use
- **In case of copyright disputes, relevant content will be removed immediately**

### ⚖️ Legal Compliance

- This project strictly complies with relevant laws, regulations and user agreements
- Users should comply with local laws and regulations when using this project
- This project assumes no legal responsibility for users' actions
- **If Tencent believes there is infringement, please contact us for immediate resolution**

### 🎯 Terms of Use

- This project is for learning, research and personal use only
- Prohibited for any commercial purposes or profit-making activities
- Users should bear the risks and legal responsibilities of use
- Please comply with WeChat user agreements and related terms of service

## Related Links

- [WeChat Official Website](https://weixin.qq.com/)
- [Selkies WebRTC](https://github.com/selkies-project)
- [LinuxServer.io](https://github.com/linuxserver)
- [xiaoheiCat/docker-wechat-sogou-pinyin](https://github.com/xiaoheiCat/docker-wechat-sogou-pinyin)

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=nickrunning/wechat-selkies&type=Date)](https://www.star-history.com/#nickrunning/wechat-selkies&Date)
