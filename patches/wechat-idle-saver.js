/*
 * 空闲自动省流：页面切到后台（document.hidden）或超过 60 秒没有任何鼠标/键盘
 * 输入后，自动把 Selkies 编码临时降到「省流」档（12 fps、2 Mbps、静态 CRF 33）；
 * 恢复可见、聚焦或任意输入后，立即还原为进入空闲前的真实设置，且永不覆盖用户
 * 手动保存的画质预设（localStorage 的 wechatQualityPreset 键）。
 *
 * 四点设计说明：
 *
 * 1. 为什么是独立文件：本仓库对「脚本 A 临时接管脚本 B 名下资源」的既定约定是
 *    独立文件 + 复用 DOM id / localStorage 键 / postMessage 通道，不做跨文件
 *    函数调用——先例是 wechat-desktop-export.js 在拖拽期间临时隐藏
 *    #wechat-quality-presets。因此本文件不调用任何其他 patch 脚本的函数，
 *    keyPrefix() / selkiesKey() / now() / ensureTopbar() 从
 *    wechat-quality-presets.js 逐字复制（仓库约定，非重复造轮子）。
 *
 * 2. 为什么不监听 window blur：blur 的唯一正确效果是「停止刷新活动时间戳」，
 *    而这不需要任何代码就自动发生——窗口失焦后不再派发任何输入事件，
 *    bumpActivity 不再被调用，时间戳自然停摆，60 秒计时器随后完成挂起。挂一个
 *    空的 blur 监听器是死代码；任何「blur 立即挂起」的行为都是错的（临时失焦、
 *    DevTools 抢焦点等场景会误挂起）。
 *
 * 3. 通道与键：与画质预设共用 {type:"settings"} postMessage（键级合并，只发本
 *    文件要动的字段）和三个 localStorage 键 framerate / video_bitrate /
 *    h264_paintover_crf。这三个键与锁定设置锁定的 11 个键零交集；本文件也刻意
 *    不写 encoder / rate_control_mode，避免与锁定设置打架。快照直接读这三个键
 *    的现值，不经过任何「预设」概念，因此无论用户选的是哪个档位还是手动拖的
 *    滑块，挂起后都能精确还原。
 *
 * 4. SUSPEND_SETTINGS 必须与 wechat-quality-presets.js 的 PRESETS[0]（省流：
 *    12 fps / 2 Mbps / 静态 CRF 33）保持同步——日后调整预设档位时两处一起改。
 */
(function () {
  "use strict";

  var TAG = "[wechat-idle-saver]";

  // 只读视图（#shared 分享页 / #player 播放页）没有控制权，与其余注入脚本一致。
  if (String(location.hash).indexOf("shared") !== -1 ||
      String(location.hash).indexOf("player") !== -1) {
    return;
  }
  if (window.wechatIdleSaverInstalled) return;
  window.wechatIdleSaverInstalled = true;

  var TOPBAR_ID = "wechat-topbar";
  var TOGGLE_ID = "wechat-idle-saver-toggle";
  var ENABLED_KEY = "wechatIdleSaverEnabled";
  var IDLE_TIMEOUT_MS = 60000;      // 无输入超过 60 秒判定为空闲
  var CHECK_INTERVAL_MS = 1000;     // 每秒检查一次空闲状态
  var ACTIVITY_THROTTLE_MS = 200;   // mousemove 等高频事件的时间戳刷新节流

  // 与 wechat-quality-presets.js 的 PRESETS[0]（省流）保持一致，见文件头第 4 点。
  var SUSPEND_SETTINGS = {
    framerate: 12,
    video_bitrate: 2,
    h264_paintover_crf: 33
  };

  var GREY = "#8b949e";     // 关闭
  var GREEN = "#3fb950";    // 开启，正常
  var AMBER = "#d29922";    // 省电中

  var enabled = readEnabled();
  var suspended = false;
  var snapshot = null;
  var lastActivityAt = now();

  var toggle = null;
  var toggleDot = null;
  var toggleLabel = null;

  /* 以下四个函数从 wechat-quality-presets.js 逐字复制，见文件头第 1 点。 */

  function keyPrefix() {
    return window.location.href.split("#")[0].replace(/[^a-zA-Z0-9.-_]/g, "_");
  }

  function selkiesKey(name) {
    return keyPrefix() + "_" + name;
  }

  function now() {
    if (window.performance && typeof window.performance.now === "function") {
      return window.performance.now();
    }
    return Date.now();
  }

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

  /* ------------------------------------------------------- 开关持久化 */

  // 默认开启：键不存在（首次使用或旧版本升级）视为 true，否则严格按 "1" 判定。
  function readEnabled() {
    try {
      var stored = window.localStorage.getItem(ENABLED_KEY);
      return stored === null ? true : stored === "1";
    } catch (e) {
      return true;   // 私有模式等异常场景退化为默认开启
    }
  }

  function persistEnabled(value) {
    try {
      window.localStorage.setItem(ENABLED_KEY, value ? "1" : "0");
    } catch (e) {
      /* 私有模式，静默 */
    }
  }

  /* ------------------------------------------------------- 设置读写 */

  function readSetting(name) {
    try {
      return window.localStorage.getItem(selkiesKey(name));
    } catch (e) {
      return null;   // 读取失败按「未知」处理，宁可保持省流值也不写坏值
    }
  }

  // 只写传入的字段：每个值 String() 后落盘，再通过 settings 通道发键级合并
  // 消息让 bundle 即时生效。刻意不带 encoder / rate_control_mode。
  function writeSettings(values) {
    for (var name in values) {
      if (!Object.prototype.hasOwnProperty.call(values, name)) continue;
      try {
        window.localStorage.setItem(selkiesKey(name), String(values[name]));
      } catch (e) {
        /* 私有模式，静默 */
      }
    }
    window.postMessage({ type: "settings", settings: values }, window.location.origin);
  }

  /* ----------------------------------------------------------- 状态机 */

  // 关键陷阱：Number(null) === 0 而不是 NaN，所以不能用 isFinite(Number(...))
  // 判断快照有效性——必须在转 Number 之前显式判 !== null，三个字段都判。
  function validSnapshot(snap) {
    return !!snap &&
      snap.framerate !== null &&
      snap.video_bitrate !== null &&
      snap.h264_paintover_crf !== null;
  }

  function suspend() {
    if (suspended || !enabled) return;
    // 快照直接现读三个键的当前值，不经过任何「预设」概念。
    snapshot = {
      framerate: readSetting("framerate"),
      video_bitrate: readSetting("video_bitrate"),
      h264_paintover_crf: readSetting("h264_paintover_crf")
    };
    suspended = true;
    writeSettings(SUSPEND_SETTINGS);
    paint();
  }

  function resume() {
    if (!suspended) return;
    suspended = false;
    var restore = snapshot;
    snapshot = null;
    if (validSnapshot(restore)) {
      // 快照是字符串，写回时转回数值再 String()，确保与其它注入脚本写入格式一致。
      writeSettings({
        framerate: Number(restore.framerate),
        video_bitrate: Number(restore.video_bitrate),
        h264_paintover_crf: Number(restore.h264_paintover_crf)
      });
    } else {
      // 快照不完整（读取失败等）：不写入，宁可保持省流值也不能写 NaN。
      console.warn(TAG, "idle snapshot incomplete; keeping savings values");
    }
    paint();
  }

  function bumpActivity() {
    if (suspended) {
      // 挂起状态下任何输入都立即恢复，节流不适用于恢复路径。
      lastActivityAt = now();
      resume();
      return;
    }
    // 未挂起时按 ACTIVITY_THROTTLE_MS 节流刷新时间戳，避免高频事件刷屏。
    if (now() - lastActivityAt >= ACTIVITY_THROTTLE_MS) {
      lastActivityAt = now();
    }
  }

  function tick() {
    ensureToggle();   // 自愈：按钮被外部代码移除时重建
    if (!enabled || suspended) return;
    // 页面切到后台时不依赖时间戳：后台标签页的定时器会被浏览器节流，直接按
    // 已超时处理，让 visibilitychange 的立即 tick() 能马上挂起。
    var idleMs = document.hidden ? IDLE_TIMEOUT_MS : (now() - lastActivityAt);
    if (idleMs >= IDLE_TIMEOUT_MS) suspend();
  }

  /* ------------------------------------------------------------------ UI */

  // 幂等：已存在则复用并重新绑定引用，否则创建一个真正的 <button type="button">
  // （圆点 + 文字 pill，视觉语言与 wechat-connection-status.js 一致），追加到
  // 共享 #wechat-topbar 末尾。
  function ensureToggle() {
    var node = document.getElementById(TOGGLE_ID);
    if (node) {
      toggle = node;
      toggleDot = node.children[0];
      toggleLabel = node.children[1];
      return node;
    }
    node = document.createElement("button");
    node.id = TOGGLE_ID;
    node.type = "button";
    node.style.cssText = [
      "appearance:none", "border:0", "border-radius:999px",
      "padding:4px 10px", "background:rgba(0,0,0,.65)", "color:#fff",
      "cursor:pointer", "display:flex", "align-items:center", "gap:6px",
      "font:12px system-ui,-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif",
      "white-space:nowrap", "user-select:none"
    ].join(";");
    node.setAttribute("aria-pressed", enabled ? "true" : "false");

    toggleDot = document.createElement("span");
    toggleDot.style.cssText = [
      "width:8px", "height:8px", "border-radius:50%", "flex:0 0 auto"
    ].join(";");

    toggleLabel = document.createElement("span");

    node.appendChild(toggleDot);
    node.appendChild(toggleLabel);
    node.addEventListener("click", onToggleClick);
    ensureTopbar().appendChild(node);
    toggle = node;
    paint();
    return node;
  }

  // 三态：关闭 = GREY/「自动省流：关」；开启未挂起 = GREEN/「自动省流」；
  // 省电中 = AMBER/「省电中」。aria-pressed 只反映开关这个持久状态，不反映
  // 挂起子状态。
  function paint() {
    if (!toggle || !toggleDot || !toggleLabel) return;
    var colour;
    var text;
    if (!enabled) {
      colour = GREY;
      text = "自动省流：关";
    } else if (suspended) {
      colour = AMBER;
      text = "省电中";
    } else {
      colour = GREEN;
      text = "自动省流";
    }
    toggleDot.style.background = colour;
    toggleLabel.textContent = text;
    toggle.setAttribute("aria-pressed", enabled ? "true" : "false");
  }

  function onToggleClick() {
    if (enabled) {
      // 关闭：若正挂起先还原快照，再关闭。
      if (suspended) resume();
      enabled = false;
    } else {
      // 开启：先刷新时间戳，获得全新宽限期，再开启。
      lastActivityAt = now();
      enabled = true;
    }
    persistEnabled(enabled);
    paint();
  }

  /* -------------------------------------------------------------- 安装 */

  function install() {
    ensureToggle();

    // 变 hidden 时立即 tick()（不等下一秒节拍）；变可见时视为一次活动。
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) {
        tick();
      } else {
        bumpActivity();
      }
    });
    window.addEventListener("focus", bumpActivity);
    // 键鼠输入即活动。全部被动监听，不阻塞事件本身。
    ["mousemove", "mousedown", "keydown", "wheel", "touchstart"].forEach(
      function (type) {
        document.addEventListener(type, bumpActivity, { passive: true });
      }
    );
    // 刻意不监听 window blur，原因见文件头第 2 点。

    setInterval(tick, CHECK_INTERVAL_MS);
    tick();
    console.log(TAG, "installed, enabled =", enabled);
  }

  function boot() {
    var tries = 0;
    var timer = setInterval(function () {
      tries++;
      if (document.body) {
        clearInterval(timer);
        install();
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
})();
