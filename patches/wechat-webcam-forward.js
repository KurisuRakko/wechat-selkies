/*
 * 浏览器 → 容器虚拟摄像头桥接（与 Selkies 数据通道完全独立）。
 *
 * Selkies 上游没有任何「浏览器摄像头 → 服务端」的能力，本镜像里它的 WebRTC
 * 代码路径甚至不会执行（基础镜像把运行模式硬编码为 --mode=websockets）。
 * 本文件是一条全新旁路，绝不接入 selkies 自己的数据 socket：
 *
 *   getUserMedia 采集 → 离屏 <canvas> 按 15fps 编码 JPEG → 专用 WebSocket
 *   （SUBFOLDER 安全）→ nginx 反代到容器内常驻的 bridge 服务
 *   （root/scripts/webcam/wechat-webcam-bridge.py，s6 服务 svc-wechat-webcam）
 *   → pyfakewebcam 写入宿主映射进来的 /dev/videoN（v4l2loopback）→ 微信
 *   「设置-视频通话」可选到该虚拟摄像头。
 *
 * 发送的是裸 JPEG 帧，不带任何类型前缀字节：bridge 端按整帧解码，省掉每帧的
 * 协议开销，也让服务端 2 MB 的 payload 上限内能装下最大号的 640x480 帧。
 *
 * 仅在开启构建参数 INSTALL_WEBCAM_FORWARD 时注入，默认构建的页面字节不变；
 * 并且只有已加载 v4l2loopback 的 Linux 宿主可用——Windows + Docker Desktop
 * （WSL2 backend）宿主无法加载自定义内核模块，不支持此功能，见
 * docs/webcam-forwarding.md。
 *
 * The #wechat-topbar host element is shared with wechat-quality-presets.js;
 * whichever script runs first creates it.
 */
(function () {
  "use strict";

  var TAG = "[wechat-webcam-forward]";

  // 只读的 #shared / #player 页面（共享观看、无输入权限）不提供摄像头转发。
  if (String(location.hash).indexOf("shared") !== -1 ||
      String(location.hash).indexOf("player") !== -1) {
    return;
  }
  if (window.wechatWebcamForwardInstalled) return;
  window.wechatWebcamForwardInstalled = true;

  var TOPBAR_ID = "wechat-topbar";
  var GROUP_ID = "wechat-webcam-forward";
  var WIDTH = 640;
  var HEIGHT = 480;
  var ACTIVE_BG = "#07c160";
  var TOAST_MS = 5000;

  // 两个旋钮只做 window 级调试钩子（与其它脚本的约定一致，不新建环境变量
  // 注入机制）：开发者可在控制台覆盖 window.WECHAT_WEBCAM_FPS /
  // WECHAT_WEBCAM_JPEG_QUALITY 再点按钮。
  function fps() {
    var v = Number(window.WECHAT_WEBCAM_FPS || 15);
    return isFinite(v) && v > 0 ? v : 15;
  }

  function jpegQuality() {
    var v = Number(window.WECHAT_WEBCAM_JPEG_QUALITY || 0.6);
    return isFinite(v) && v > 0 && v <= 1 ? v : 0.6;
  }

  // 运行期状态。全部收进闭包，页面内多次注入也互不干扰。
  var active = false;
  var stream = null;
  var socket = null;
  var timer = null;
  var video = null;
  var canvas = null;
  var button = null;

  /* ------------------------------------------------------------------ 挂载 */

  function ensureTopbar() {
    var bar = document.getElementById(TOPBAR_ID);
    if (bar) return bar;
    bar = document.createElement("div");
    bar.id = TOPBAR_ID;
    bar.style.cssText = [
      "position:fixed", "top:8px", "right:20px", "z-index:1051",
      "display:flex", "align-items:center", "gap:8px",
      "pointer-events:auto"
    ].join(";");
    document.body.appendChild(bar);
    return bar;
  }

  // 与 #wechat-quality-presets 同款 pill 样式，但只有一个开关按钮。
  function build() {
    if (document.getElementById(GROUP_ID)) return null;

    var group = document.createElement("div");
    group.id = GROUP_ID;
    group.setAttribute("role", "group");
    group.setAttribute("aria-label", "摄像头转发");
    group.style.cssText = [
      "display:flex", "align-items:center", "gap:2px",
      "padding:2px", "border-radius:999px",
      "background:rgba(0,0,0,.65)",
      "font:12px system-ui,-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif",
      "user-select:none"
    ].join(";");

    var btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = "摄像头";
    btn.title = "浏览器摄像头 → 微信视频通话（需要宿主已加载 v4l2loopback）";
    btn.style.cssText = [
      "appearance:none", "border:0", "border-radius:999px",
      "padding:3px 9px", "background:transparent", "color:#ddd",
      "cursor:pointer", "font:inherit", "line-height:1.4"
    ].join(";");
    btn.setAttribute("aria-pressed", "false");
    btn.addEventListener("click", toggle);
    group.appendChild(btn);
    ensureTopbar().appendChild(group);
    console.log(TAG, "installed");
    return btn;
  }

  function boot() {
    var tries = 0;
    var timer = setInterval(function () {
      tries++;
      if (document.body) {
        clearInterval(timer);
        button = build();
      } else if (tries > 120) {
        clearInterval(timer);
        console.warn(TAG, "gave up waiting for document.body");
      }
    }, 500);
  }

  if (document.readyState === "loading" && !document.body) {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }

  /* ------------------------------------------------------------------ toast */

  // 镜像 wechat-dragdrop.js 的 toast()，保持视觉一致；不要去改那个文件的
  // 导出面。
  function toast(text) {
    if (!document.body) return;
    var bar = document.createElement("div");
    bar.setAttribute("role", "status");
    bar.style.cssText = [
      "position:fixed", "bottom:16px", "right:20px", "z-index:2147483646",
      "max-width:min(80vw,420px)", "box-sizing:border-box",
      "padding:9px 13px", "border-radius:8px",
      "background:rgba(31,31,31,.94)", "color:#fff",
      "font:13px/1.4 system-ui,-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif",
      "box-shadow:0 4px 16px rgba(0,0,0,.45)",
      "word-break:break-all", "pointer-events:none"
    ].join(";");
    bar.textContent = text;
    document.body.appendChild(bar);
    setTimeout(function () {
      if (bar.parentNode) bar.parentNode.removeChild(bar);
    }, TOAST_MS);
  }

  /* ------------------------------------------------------------ WebSocket */

  // SUBFOLDER 安全：沿用 wechat-dragdrop.js attachUrl() 的目录前缀手法，
  // 改造成绝对 ws(s):// 地址，与 nginx 注入的 location SUBFOLDERwechat-webcam/
  // 对应（见 patches/install-wechat-webcam-forward.sh）。
  function webcamWsUrl() {
    var scheme = location.protocol === "https:" ? "wss:" : "ws:";
    return scheme + "//" + location.host +
      location.pathname.replace(/[^/]*$/, "") + "wechat-webcam/";
  }

  /* -------------------------------------------------------------- 帧采集 */

  function drawFrame() {
    if (!canvas || !video || !socket) return;
    var ctx = canvas.getContext("2d");
    if (!ctx || typeof ctx.drawImage !== "function") return;
    ctx.drawImage(video, 0, 0, WIDTH, HEIGHT);
    if (socket.readyState !== WebSocket.OPEN) return;
    canvas.toBlob(function (blob) {
      if (!blob || !socket || socket.readyState !== WebSocket.OPEN) return;
      blob.arrayBuffer().then(function (buffer) {
        if (!socket || socket.readyState !== WebSocket.OPEN) return;
        try {
          socket.send(buffer);
        } catch (e) {
          /* 连接恰好在 send 前后关闭，onclose 会兜底清理 */
        }
      });
    }, "image/jpeg", jpegQuality());
  }

  /* ------------------------------------------------------------ 开关流程 */

  function start() {
    var md = navigator.mediaDevices;
    if (!md || typeof md.getUserMedia !== "function") {
      console.warn(TAG, "navigator.mediaDevices.getUserMedia unavailable");
      toast("当前浏览器不支持摄像头采集");
      return;
    }
    md.getUserMedia({
      video: {
        width: { ideal: 640 },
        height: { ideal: 480 },
        frameRate: { ideal: 15, max: 15 }
      }
    }).then(function (s) {
      // 双保险：连点或上一轮收尾未完成时，不叠第二条流。
      if (active) {
        s.getTracks().forEach(function (track) { track.stop(); });
        return;
      }
      var sock;
      try {
        sock = new WebSocket(webcamWsUrl());
      } catch (error) {
        s.getTracks().forEach(function (track) { track.stop(); });
        console.warn(TAG, "could not create WebSocket", error);
        return;
      }
      stream = s;
      socket = sock;
      active = true;
      video = document.createElement("video");
      video.srcObject = s;
      if (typeof video.play === "function") video.play();
      canvas = document.createElement("canvas");
      canvas.width = WIDTH;
      canvas.height = HEIGHT;
      timer = setInterval(drawFrame, Math.round(1000 / fps()));
      socket.onclose = handleDisconnect;
      socket.onerror = function () {
        // 错误之后浏览器必然补发 close，toast 只在 handleDisconnect 里出一次。
        console.warn(TAG, "websocket error");
      };
      if (button) {
        button.style.background = ACTIVE_BG;
        button.style.color = "#fff";
        button.style.fontWeight = "600";
        button.setAttribute("aria-pressed", "true");
      }
      console.log(TAG, "forwarding to", webcamWsUrl());
    }).catch(function (error) {
      console.warn(TAG, "getUserMedia failed", error);
      toast("无法访问摄像头: " + (error && error.name ? error.name : "拒绝访问"));
    });
  }

  // 幂等收尾：按钮关闭与连接断开共用，谁先到都安全。
  function teardown() {
    if (timer) {
      clearInterval(timer);
      timer = null;
    }
    if (stream) {
      stream.getTracks().forEach(function (track) {
        try {
          track.stop();
        } catch (e) {
          /* track 已被系统回收 */
        }
      });
      stream = null;
    }
    if (video) {
      try {
        video.srcObject = null;
      } catch (e) {
        /* noop */
      }
      video = null;
    }
    if (socket) {
      var sock = socket;
      socket = null;
      // 先摘 handler 再 close：避免 close() 触发 onclose 再走一遍断开提示。
      sock.onclose = null;
      sock.onerror = null;
      try {
        sock.close();
      } catch (e) {
        /* noop */
      }
    }
    canvas = null;
    if (button) {
      button.style.background = "transparent";
      button.style.color = "#ddd";
      button.style.fontWeight = "400";
      button.setAttribute("aria-pressed", "false");
    }
    active = false;
  }

  function stopByUser() {
    console.log(TAG, "stopping forwarding");
    teardown();
  }

  function handleDisconnect() {
    toast("摄像头转发已断开");
    teardown();
  }

  function toggle() {
    if (active) {
      stopByUser();
    } else {
      start();
    }
  }
})();
