# 浏览器摄像头转发（可选，仅 Linux 宿主）

一句话结论：**Windows + Docker Desktop（WSL2 backend）宿主不支持此功能**——
v4l2loopback 是 out-of-tree 内核模块，必须加载进宿主内核，而 WSL2 不支持加载
自定义内核模块（官方只支持整体替换内核，且明确 not officially supported）。

该功能是一条与 Selkies 数据通道完全独立的旁路，默认构建保持关闭：浏览器用
getUserMedia 采集摄像头，离屏 canvas 按 15fps 编码成 JPEG，经专用 WebSocket
（nginx 反代）送入容器内常驻的 bridge 服务（`/scripts/webcam/wechat-webcam-bridge.py`，
s6 服务 `svc-wechat-webcam`），由 pyfakewebcam 写入宿主映射进来的 `/dev/videoN`
虚拟摄像头，微信的「设置-视频通话」即可选用它。

## 宿主前提条件

- **Linux 宿主**，且内核已加载 `v4l2loopback` 模块（见下文启用步骤）。
- Windows + Docker Desktop（WSL2 backend）：**不支持**。v4l2loopback 必须以
  内核模块形式加载进宿主内核，WSL2 不允许加载自定义内核模块，因此该功能在
  此类宿主上无法工作；请勿试图在 WSL2 里 modprobe。
- 构建期开关 `INSTALL_WEBCAM_FORWARD` 默认 `false`，默认构建的镜像里不存在
  任何本功能相关文件。

## 安装与首次配置

在 Linux 宿主上按顺序执行：

1. 加载 v4l2loopback 并创建一个虚拟摄像头：

   ```sh
   sudo modprobe v4l2loopback video_nr=10 card_label="SelkiesWebcam" exclusive_caps=1
   ```

   `exclusive_caps=1` 是必要参数：它让设备只暴露 CAPTURE 能力，Chromium/Qt
   一类的应用（微信的界面层）才能把它识别为可用的摄像头；不设这个参数时
   设备同时暴露 OUTPUT 能力，很多应用会直接忽略它。

2. 确认设备节点出现：

   ```sh
   ls -l /dev/video10
   ```

3. 用开启开关构建镜像：

   ```sh
   docker build --build-arg INSTALL_WEBCAM_FORWARD=true -t wechat-selkies:webcam .
   ```

4. 在 compose 里解注设备映射（`docker-compose.yml` 的 `devices` 块）并启动：

   ```yaml
   devices:
     - /dev/dri:/dev/dri
     - /dev/video10:/dev/video10
   ```

5. 浏览器打开页面后点击顶部右上角的「摄像头」按钮，允许浏览器使用摄像头。
6. 打开微信「设置 → 视频通话」，在摄像头下拉列表里选择 `SelkiesWebcam`
   （名称由 `card_label` 决定，见第 1 步）。

顺序不能反：虚拟摄像头是宿主侧的常驻设备，与容器启动先后无关；但 bridge 是
s6 服务，容器启动后即常驻运行，因此浏览器点按钮时服务一定已经就绪。

## 环境变量

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `WECHAT_WEBCAM_DEVICE` | `/dev/video10` | 虚拟摄像头设备节点（须由宿主映射进容器） |
| `WECHAT_WEBCAM_WIDTH` | `640` | 输出宽度 |
| `WECHAT_WEBCAM_HEIGHT` | `480` | 输出高度 |
| `WECHAT_WEBCAM_BRIDGE_PORT` | `8767` | bridge 的 loopback 监听端口（nginx 反代到 `SUBFOLDERwechat-webcam/`） |

调试钩子（浏览器控制台覆盖，无环境变量注入）：`window.WECHAT_WEBCAM_FPS`
（默认 15）、`window.WECHAT_WEBCAM_JPEG_QUALITY`（默认 0.6）。

## 限制

- 单路：一次只允许一条浏览器连接；再次点击按钮会先停掉前一条链路。
- 固定 640x480@15fps 的编码约定（canvas 尺寸与 bridge 端 resize 协同）。
- 无设备热插拔处理：宿主在容器运行期间卸载/重载 v4l2loopback 后，需要重启
  容器（bridge 只在打开失败时记一次错误日志，之后安静丢帧，不会自愈）。
- 只解决「微信能读到摄像头画面」；语音/视频通话能否接通取决于微信自身的
  网络穿透，与本功能无关。

## 验证

- 构建期：安装脚本会对 bridge 做依赖 import 自检并运行 `--self-test`
  （纯解码逻辑，不碰真实设备）。
- 本地（需 numpy/Pillow）：

  ```sh
  python3 patches/test-wechat-webcam-bridge.py
  node patches/test-wechat-webcam-forward.js
  ```

- 容器内冒烟（镜像内自检）：

  ```sh
  docker run --rm --entrypoint /lsiopy/bin/python3 \
    wechat-selkies:webcam /scripts/webcam/wechat-webcam-bridge.py --self-test
  ```
