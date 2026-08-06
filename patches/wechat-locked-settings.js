/*
 * 锁定这套部署最稳定的显示/编码设置，隐藏侧边栏的“应用程序”和“共享”
 * 两块面板，并让画质滑块统一为“最左 = 画质最差、最右 = 画质最好”。
 *
 * 不只用 SELKIES_* 环境变量的原因：浏览器曾经写入 localStorage 的旧值会在
 * 每次重连时重新生效，而且 dashboard 的控件是动态渲染的。这个脚本在 bundle
 * 启动前同步写入 Selkies 实际读取的键，再用 MutationObserver 保证后渲染出来
 * 的控件和面板也会被锁定/隐藏。
 *
 * 编码器和码率控制模式刻意与 patches/wechat-quality-presets.js 保持一致；
 * 帧率/比特率仍由预设脚本负责，这里不会写这两个键。
 */
(function () {
  "use strict";

  var TAG = "[wechat-locked-settings]";

  if (String(location.hash).indexOf("shared") !== -1 ||
      String(location.hash).indexOf("player") !== -1) {
    return;
  }
  if (window.wechatLockedSettingsInstalled) return;
  window.wechatLockedSettingsInstalled = true;

  /*
   * 十个设置值都来自 src/selkies-core.js 里的真实键名：
   * - HiDPI 的反向开关会同时写 use_css_scaling（dashboard UI）和
   *   useCssScaling（core 启动读取），false = 客户端像素完美画布。
   * - 175% 对应 scaling_dpi 枚举里的 DPI 168。
   * - Turbo 和 CPU 编码以 "true"/"false" 字符串存储。
   */
  var LOCKED_SETTINGS = {
    use_css_scaling: "false",
    useCssScaling: "false",
    force_aligned_resolution: "true",
    antiAliasingEnabled: "true",
    use_browser_cursors: "true",
    scaling_dpi: "168",
    encoder: "x264enc-striped",
    rate_control_mode: "cbr",
    use_paint_over_quality: "true",
    h264_streaming_mode: "false",
    use_cpu: "false"
  };

  var TOGGLE_STATES = [
    { id: "hidpiToggle", value: true },
    { id: "forceAlignedResolutionToggle", value: true },
    { id: "antiAliasingToggle", value: true },
    { id: "useBrowserCursorsToggle", value: true },
    { id: "usePaintOverQualityToggle", value: true },
    { id: "h264StreamingModeToggle", value: false },
    { id: "useCpuToggle", value: false }
  ];

  var SELECT_VALUES = {
    encoderSelect: "x264enc-striped",
    rateControlSelect: "cbr",
    uiScalingSelect: "168"
  };
  var LOCKED_SELECT_IDS = [
    "encoderSelect",
    "encoderRTCSelect",
    "rateControlSelect",
    "uiScalingSelect"
  ];

  var HIDDEN_SECTIONS = ["apps-content", "sharing-content"];

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
      scaling_dpi: 168,
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

  function addClass(el, className) {
    if (!el) return;
    if (el.classList && typeof el.classList.add === "function") {
      el.classList.add(className);
      return;
    }
    if (!hasClass(el, className)) {
      el.className = (String(el.className || "") + " " + className).trim();
    }
  }

  function removeClass(el, className) {
    if (!el) return;
    if (el.classList && typeof el.classList.remove === "function") {
      el.classList.remove(className);
      return;
    }
    el.className = String(el.className || "")
      .split(/\s+/)
      .filter(function (name) { return name !== className; })
      .join(" ");
  }

  function setImportantStyle(el, property, value) {
    try {
      el.style.setProperty(property, value, "important");
    } catch (e) {
      el.style[property] = value;
    }
  }

  function lockControl(el) {
    if (!el) return;
    el.disabled = true;
    if (el.getAttribute("aria-disabled") !== "true") {
      el.setAttribute("aria-disabled", "true");
    }
    if (el.getAttribute("data-wechat-locked") !== "true") {
      el.setAttribute("data-wechat-locked", "true");
    }
    if (el.style.pointerEvents !== "none") el.style.pointerEvents = "none";
    if (el.style.opacity !== "0.55") el.style.opacity = "0.55";
    if (el.style.cursor !== "not-allowed") el.style.cursor = "not-allowed";
  }

  function setToggleState(el, value) {
    if (!el) return;
    if (value && !hasClass(el, "active")) {
      addClass(el, "active");
    } else if (!value && hasClass(el, "active")) {
      removeClass(el, "active");
    }
    if (el.getAttribute("aria-pressed") !== String(value)) {
      el.setAttribute("aria-pressed", String(value));
    }
    if (el.getAttribute("data-wechat-locked-value") !== String(value)) {
      el.setAttribute("data-wechat-locked-value", String(value));
    }
  }

  function hasOption(el, value) {
    if (!el.options || !el.options.length) return true;
    for (var i = 0; i < el.options.length; i++) {
      if (el.options[i].value === value) return true;
    }
    return false;
  }

  function applySelectValue(el, value) {
    if (!el || !hasOption(el, value)) return;
    el.value = value;
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
    if (section.getAttribute("hidden") === null) {
      section.setAttribute("hidden", "");
    }
    if (section.getAttribute("data-wechat-hidden") !== ariaControls) {
      section.setAttribute("data-wechat-hidden", ariaControls);
    }
    if (section.style.display !== "none") {
      setImportantStyle(section, "display", "none");
    }
  }

  function applyDom() {
    for (var i = 0; i < TOGGLE_STATES.length; i++) {
      var toggle = TOGGLE_STATES[i];
      var node = document.getElementById(toggle.id);
      lockControl(node);
      setToggleState(node, toggle.value);
    }

    for (var j = 0; j < LOCKED_SELECT_IDS.length; j++) {
      var select = document.getElementById(LOCKED_SELECT_IDS[j]);
      lockControl(select);
      if (Object.prototype.hasOwnProperty.call(SELECT_VALUES, LOCKED_SELECT_IDS[j])) {
        applySelectValue(select, SELECT_VALUES[LOCKED_SELECT_IDS[j]]);
      }
    }

    for (var k = 0; k < HIDDEN_SECTIONS.length; k++) {
      hideSection(HIDDEN_SECTIONS[k]);
    }

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

  function boot() {
    var tries = 0;
    var timer = setInterval(function () {
      tries++;
      if (document.body) {
        clearInterval(timer);
        applyDom();
        postLive();
        observe();
        startEnforceTimer();
      } else if (tries > 120) {
        clearInterval(timer);
        console.warn(TAG, "gave up waiting for document.body");
      }
    }, 500);
  }

  seed();

  if (document.readyState === "loading" && !document.body) {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
