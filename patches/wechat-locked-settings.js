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
 */
(function () {
  "use strict";

  var TAG = "[wechat-locked-settings]";
  var PLAYER_MODE = String(location.hash).indexOf("player") !== -1;

  if (String(location.hash).indexOf("shared") !== -1) {
    return;
  }
  if (window.wechatLockedSettingsInstalled) return;
  window.wechatLockedSettingsInstalled = true;

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

  var HIDDEN_SECTIONS = ["apps-content", "sharing-content"];
  var HIDDEN_SECTION_TITLES = ["屏幕设置"];

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

  function hideSectionByTitle(title) {
    var headings = [];
    try {
      headings = document.getElementsByTagName("h3") || [];
    } catch (e) {
      headings = [];
    }
    for (var i = 0; i < headings.length; i++) {
      if (String(headings[i].textContent || "").trim() !== title) continue;
      var section = headings[i].parentNode;
      while (section && !hasClass(section, "sidebar-section")) {
        section = section.parentNode;
      }
      if (section) hideElement(section, "title:" + title);
      return;
    }
  }

  function hideGamepadButtons() {
    var elements = [];
    try {
      elements = document.getElementsByTagName("*") || [];
    } catch (e) {
      elements = [];
    }
    for (var i = 0; i < elements.length; i++) {
      var el = elements[i];
      if (!el || el.tagName !== "BUTTON") continue;
      if (hasClass(el, "player-gamepad-button") ||
          el.getAttribute("aria-label") === "Toggle Touch Gamepad" ||
          el.getAttribute("title") === "Toggle Touch Gamepad") {
        hideElement(el, "gamepad-button");
      }
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

    for (var n = 0; n < HIDDEN_SECTION_TITLES.length; n++) {
      hideSectionByTitle(HIDDEN_SECTION_TITLES[n]);
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
  function enforce() {
    seed();
    applyDom();
  }
  function startEnforceTimer() {
    if (enforceTimer) return;
    enforceTimer = setInterval(enforce, 1000);
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

  function observe() {
    if (window.wechatLockedSettingsObserver) return;
    if (typeof MutationObserver === "undefined") return;
    var observer = new MutationObserver(function () {
      scheduleApply();
    });
    observer.observe(
      document.documentElement || document.body,
      { childList: true, subtree: true }
    );
    window.wechatLockedSettingsObserver = observer;
  }

  function boot(applySettings) {
    var tries = 0;
    var timer = setInterval(function () {
      tries++;
      if (document.body) {
        clearInterval(timer);
        applyDom();
        if (applySettings) {
          postLive();
          startEnforceTimer();
        }
        observe();
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
