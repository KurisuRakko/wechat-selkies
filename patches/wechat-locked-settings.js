/*
 * 锁定这套部署最稳定的显示/编码设置，隐藏侧边栏的“应用程序”“共享”和
 * “屏幕设置”面板，隐藏 player 页面浮动手柄按钮，并让画质滑块统一为
 * “最左 = 画质最差、最右 = 画质最好”。
 *
 * 不只用 SELKIES_* 环境变量的原因：浏览器曾经写入 localStorage 的旧值会在
 * 每次重连时重新生效，而且 dashboard 的控件是动态渲染的。这个脚本在 bundle
 * 启动前同步写入 Selkies 实际读取的键，再用 MutationObserver 保证后渲染出来
 * 的控件和面板也会被锁定/隐藏。
 * #player2/3/4 页面没有 dashboard 设置，因此只隐藏浮动手柄按钮，不写设置。
 *
 * 编码器和码率控制模式刻意与 patches/wechat-quality-presets.js 保持一致；
 * 帧率/比特率仍由预设脚本负责，这里不会写这两个键。
 *
 * 加固（2026-08-10）：跨容器重启重连时，页面会在同一窗口内整体重建文档
 * （软重载），重连后侧边栏随之整棵重建。安装守卫与观察器原先挂在 window 上，
 * 文档重建后脚本重新求值会被守卫拦下、旧观察器也指向被丢弃的旧文档，隐藏
 * pass 从此失活——而锁定值靠 localStorage 持久化仍在生效，表现为“锁定了但
 * 隐藏消失”。现在守卫按文档粒度、观察器按当前文档根挂载，1 秒 enforce 节拍
 * 在根被替换时把观察器挂回新文档，player 页也常驻仅隐藏的兜底节拍。
 */
(function () {
  "use strict";

  var TAG = "[wechat-locked-settings]";
  var PLAYER_MODE = String(location.hash).indexOf("player") !== -1;

  if (String(location.hash).indexOf("shared") !== -1) {
    return;
  }
  // 文档粒度守卫：文档被整体重建（软重载/重连）后旗标随旧文档一起消失，脚本在
  // 新文档重新求值会完整重装；同一文档内的重复注入仍会被拦住。
  if (document.wechatLockedSettingsInstalled) return;
  document.wechatLockedSettingsInstalled = true;

  /*
   * 十个设置值都来自 src/selkies-core.js 里的真实键名：
   * - HiDPI 的反向开关会同时写 use_css_scaling（dashboard UI）和
   *   useCssScaling（core 启动读取），false = 客户端像素完美画布。
   * - 200% 对应 scaling_dpi 枚举里的 DPI 192。
   * - Turbo 和 CPU 编码以 "true"/"false" 字符串存储。
   */
  var LOCKED_SETTINGS = {
    use_css_scaling: "false",
    useCssScaling: "false",
    force_aligned_resolution: "true",
    antiAliasingEnabled: "true",
    use_browser_cursors: "true",
    scaling_dpi: "192",
    encoder: "x264enc-striped",
    rate_control_mode: "cbr",
    use_paint_over_quality: "true",
    h264_streaming_mode: "false",
    use_cpu: "false"
  };

  var HIDDEN_SETTING_IDS = [
    "hidpiToggle",
    "forceAlignedResolutionToggle",
    "antiAliasingToggle",
    "useBrowserCursorsToggle",
    "uiScalingSelect",
    "encoderSelect",
    "encoderRTCSelect",
    "rateControlSelect",
    "usePaintOverQualityToggle",
    "h264StreamingModeToggle",
    "useCpuToggle"
  ];

  var HIDDEN_SECTIONS = ["apps-content", "sharing-content", "screen-settings-content"];

  var CRF_SLIDER_IDS = ["videoCRFSlider", "h264PaintoverCRFSlider"];

  function keyPrefix() {
    // 与 bundle 里的实现保持一致；字符类是 RANGE ".-_"，不能“修正”它。
    return window.location.href.split("#")[0].replace(/[^a-zA-Z0-9.-_]/g, "_");
  }

  function selkiesKey(name) {
    return keyPrefix() + "_" + name;
  }

  function seed() {
    try {
      for (var name in LOCKED_SETTINGS) {
        if (Object.prototype.hasOwnProperty.call(LOCKED_SETTINGS, name)) {
          window.localStorage.setItem(selkiesKey(name), LOCKED_SETTINGS[name]);
        }
      }
    } catch (e) {
      console.warn(TAG, "could not persist locked settings", e);
    }
  }

  function postSettings() {
    var settings = {
      encoder: "x264enc-striped",
      rate_control_mode: "cbr",
      scaling_dpi: 192,
      force_aligned_resolution: true,
      use_css_scaling: false,
      use_browser_cursors: true,
      use_paint_over_quality: true,
      h264_streaming_mode: false,
      use_cpu: false
    };
    window.postMessage(
      { type: "settings", settings: settings },
      window.location.origin
    );
    // settings 通道不带抗锯齿，它有自己的独立事件。
    window.postMessage(
      { type: "setAntiAliasing", value: true },
      window.location.origin
    );
  }

  function hasClass(el, className) {
    if (el.classList && typeof el.classList.contains === "function") {
      return el.classList.contains(className);
    }
    return (" " + String(el.className || "") + " ").indexOf(" " + className + " ") !== -1;
  }

  function setImportantStyle(el, property, value) {
    try {
      el.style.setProperty(property, value, "important");
    } catch (e) {
      el.style[property] = value;
    }
  }

  /*
   * CRF 数值越小画质越好。Chromium 的 range input 在 direction:rtl 下会把
   * min 放在右侧、max 放在左侧，因此滑块天然就是“最左 = 最差、最右 = 最好”，
   * React 状态、localStorage 和旁边的数字都继续保留真实 CRF，不需要拦截事件。
   */
  function normalizeCrfSlider(slider) {
    if (!slider) return;
    if (slider.style.direction !== "rtl") slider.style.direction = "rtl";
  }

  function hideSection(ariaControls) {
    var header = null;
    try {
      header = document.querySelector('[aria-controls="' + ariaControls + '"]');
    } catch (e) {
      header = null;
    }
    if (!header) return;

    var section = header.parentNode;
    while (section && !hasClass(section, "sidebar-section")) {
      section = section.parentNode;
    }
    if (!section) return;
    hideElement(section, ariaControls);
  }

  function hideElement(element, marker) {
    if (element.getAttribute("hidden") === null) {
      element.setAttribute("hidden", "");
    }
    if (element.getAttribute("data-wechat-hidden") !== marker) {
      element.setAttribute("data-wechat-hidden", marker);
    }
    if (element.style.display !== "none") {
      setImportantStyle(element, "display", "none");
    }
  }

  function hideGamepadButtons() {
    var buttons = [];
    try {
      buttons = document.querySelectorAll(
        'button.player-gamepad-button, ' +
        'button[aria-label="Toggle Touch Gamepad"], ' +
        'button[title="Toggle Touch Gamepad"]'
      ) || [];
    } catch (e) {
      buttons = [];
    }
    for (var i = 0; i < buttons.length; i++) {
      hideElement(buttons[i], "gamepad-button");
    }
  }

  function findSettingRow(control) {
    var node = control;
    while (node) {
      if (hasClass(node, "dev-setting-item")) return node;
      node = node.parentNode;
    }
    return null;
  }

  function hideSettingRow(controlId) {
    var control = document.getElementById(controlId);
    if (!control) return;
    var row = findSettingRow(control);
    if (!row) return;
    hideElement(row, "setting:" + controlId);
  }

  function hideSettingsRows() {
    for (var i = 0; i < HIDDEN_SETTING_IDS.length; i++) {
      hideSettingRow(HIDDEN_SETTING_IDS[i]);
    }
  }

  function applyDom() {
    hideSettingsRows();

    for (var k = 0; k < HIDDEN_SECTIONS.length; k++) {
      hideSection(HIDDEN_SECTIONS[k]);
    }

    hideGamepadButtons();

    for (var m = 0; m < CRF_SLIDER_IDS.length; m++) {
      normalizeCrfSlider(document.getElementById(CRF_SLIDER_IDS[m]));
    }
  }

  var livePosted = false;
  function postLive() {
    if (livePosted) return;
    livePosted = true;
    window.setTimeout(postSettings, 0);
  }

  var enforceTimer = null;
  // dashboard 页写设置，player 页只隐藏不写设置——两种节拍共用“根变了就重挂
  // 观察器”的自愈检查，保证隐藏 pass 对任何后续重渲染常驻。
  function enforce(applySettings) {
    if (observedRoot !== currentRoot()) {
      observe();
    }
    if (applySettings) seed();
    applyDom();
  }
  function startEnforceTimer(applySettings) {
    if (enforceTimer) return;
    enforceTimer = setInterval(function () {
      enforce(applySettings);
    }, 1000);
  }

  var applyPending = false;
  function flushApply() {
    if (!applyPending) return;
    applyPending = false;
    applyDom();
  }
  function scheduleApply() {
    if (applyPending) return;
    applyPending = true;
    if (window.requestAnimationFrame) {
      window.requestAnimationFrame(flushApply);
    } else {
      window.setTimeout(flushApply, 0);
    }
  }

  // 观察器挂在“当前文档的根”上并记录 observedRoot：文档整体重建后旧观察器指向
  // 被丢弃的旧文档，enforce 节拍发现根变了就重新挂载。观察器本身永不
  // disconnect，生命周期与所在文档一致；挂 documentElement 而非 body，body 被
  // 整体替换时观察器仍然活着。
  var observedRoot = null;
  function currentRoot() {
    return document.documentElement || document.body;
  }
  function observe() {
    var root = currentRoot();
    if (observedRoot === root) return;
    if (typeof MutationObserver !== "undefined") {
      var observer = new MutationObserver(function () {
        scheduleApply();
      });
      observer.observe(root, { childList: true, subtree: true });
    }
    observedRoot = root;
  }

  function boot(applySettings) {
    var tries = 0;
    var timer = setInterval(function () {
      tries++;
      if (document.body) {
        clearInterval(timer);
        applyDom();
        observe();
        startEnforceTimer(applySettings);
        if (applySettings) {
          postLive();
        }
      } else if (tries > 120) {
        clearInterval(timer);
        console.warn(TAG, "gave up waiting for document.body");
      }
    }, 500);
  }

  if (!PLAYER_MODE) {
    seed();
  }

  if (document.readyState === "loading" && !document.body) {
    document.addEventListener("DOMContentLoaded", function () {
      boot(!PLAYER_MODE);
    });
  } else {
    boot(!PLAYER_MODE);
  }
})();
