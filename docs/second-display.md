# 副屏窗口管家（实验性）

一句话结论：**打开第二个 selkies 浏览器窗口后，微信的小程序/图片查看器一类
非主窗口会自动搬过去平铺；关掉那个窗口，它们会自动收回主屏**——这是一个
容器内常驻的"窗口管家"，建立在 Selkies 上游本来就有的多显示器能力之上，
不需要额外的显卡、虚拟设备或宿主内核模块。

## 这是什么、不是什么

Selkies 上游支持"第二显示器"：浏览器访问 `#display2` 会作为一个独立的
逻辑显示器接入同一个容器桌面，服务端用 `xrandr --setmonitor` 划出对应的
屏幕区域、单独起一路视频流。但上游不管"哪些窗口该出现在副屏里"——这正是
本功能要做的事：容器内一个常驻守护进程通过 RandR 感知副屏是否存在，把
微信的非主窗口、非模态子窗口（小程序、图片/视频查看器、独立聊天窗）搬到
副屏区域自动平铺；副屏消失时，这些窗口全部收回主屏。

不做的事：

- 不会自动弹出副屏窗口——必须由用户在页面顶部的提示条上手动点击，这是一次
  真实的用户手势，浏览器不会拦截。
- 不会搬动微信主窗口、登录二维码窗、强制登出后的提示弹窗——这些窗口的识别
  规则和 `wechat-window-watchdog.sh`（主窗口看门狗）完全一致，两者不会对
  "这是不是主窗口"产生分歧。
- v1 只支持一块副屏（和上游现状一致：同一时刻只允许一个非主显示器存在）。
- 不会修改微信主窗口的最大化/自动重新登录逻辑，那是看门狗的职责。

## 启用步骤

1. 构建或拉取镜像时不需要任何额外的 `--build-arg`——本功能的资源随镜像
   无条件存在，只在运行期由环境变量决定启不启用。
2. 在 `docker-compose.yml` 或 `.env` 里设置：

   ```yaml
   environment:
     - ENABLE_WECHAT_SECOND_DISPLAY=true
   ```

3. 启动/重启容器。
4. 浏览器打开微信桌面页面（主窗口），操作出至少一个非主窗口（比如打开一个
   小程序）。页面顶部会出现一条提示："检测到 N 个可弹出的微信窗口 · 点击
   在副屏打开"。
5. 点击提示条：浏览器会打开一个新窗口/标签页（`#display2`），这就是副屏。
   稍等片刻，非主窗口会自动搬过去、按网格平铺。
6. 关掉副屏窗口：非主窗口会在数秒内自动收回主屏画面。

## 环境变量

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `ENABLE_WECHAT_SECOND_DISPLAY` | `false` | 总开关。关闭时容器内的窗口管家进程常驻但不做任何事，也不监听端口。 |

## 已知限制

- **副屏连接/断开/改分辨率会让主屏视频短暂重置一次**——这是 Selkies 上游
  `reconfigure_displays()` 的既有行为（停止全部视频流、重新划分 RandR 布局、
  重新起流），不是本功能引入的新问题，点击提示条打开副屏或关闭副屏窗口时
  预期会看到主屏画面短暂闪一下。
- v1 只支持一块副屏；上游本身"同一时刻只允许一个非 primary 逻辑显示器"，
  真正的多路独立显示器需要先改造上游（`selkies.py` 的 display_id 白名单、
  单副屏踢除逻辑），不在本次范围内。
- 不与 `HARDEN_DESKTOP` 联动——它的强化分支只涉及文件传输/命令执行/侧边栏，
  和窗口管家没有交集，两者可以同时开启。
- `wechat-idle-saver.js` / `wechat-quality-presets.js` / `wechat-locked-settings.js`
  的画质相关 localStorage 键目前是按 URL 前缀（不区分 displayId）存储的，
  主屏和副屏窗口之间可能互相影响画质设置——这是上游/既有实现的既定缺口，
  不在本次修复范围内。
- 状态端点只暴露计数和几何数字（`movable_count`/`unassigned_count`/显示器
  坐标），不包含任何窗口标题或 WM_CLASS 原文，避免通过网络暴露小程序名称
  一类的可辨识信息。

## 验证

- 纯函数单测（分类规则、平铺/级联布局，脱离 X 服务器）：

  ```sh
  python3 -m pytest patches/test-second-display-classify.py -v
  ```

- 客户端脚本（DOM 级 node:vm 测试）：

  ```sh
  node --check patches/wechat-second-display.js
  node patches/test-wechat-second-display.js
  ```

- 构建产物只读断言（nginx 反代、注入脚本、s6 服务文件是否就位）：

  ```sh
  docker build -t wechat-selkies:second-display-test .
  docker run --rm --entrypoint bash wechat-selkies:second-display-test -c '
    test "$(grep -c "location SUBFOLDERwechat-second-display/" /defaults/default.conf)" = "2"
    test "$(grep -c "wechat-second-display.js" /usr/share/selkies/selkies-dashboard/index.html)" = "1"
    test -x /etc/s6-overlay/s6-rc.d/svc-wechat-second-display/run
    echo OK'
  ```

- 一次性容器里的 X11 集成测试（真造窗口、真跑 xrandr、文本断言几何）：

  ```sh
  python3 patches/test-second-display-x11.py
  ```

- 端到端冒烟（唯一允许人工目视的一项）：`docker compose up` 后开一个小程序
  或图片查看器，等提示条出现，点击打开副屏，确认子窗口平铺过去；关闭副屏
  窗口，确认子窗口收回主屏。
