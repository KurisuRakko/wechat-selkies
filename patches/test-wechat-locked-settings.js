#!/usr/bin/env node
"use strict";

// 纯文本 DOM 测试：不使用浏览器和截图。脚本在 node:vm 里跑在最小假 DOM 上，
// 记录 localStorage 写入、postMessage 调用、控件禁用状态和 MutationObserver
// 回调。

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

/* ------------------------------------------------------------ 假定时器 */

let sequence = 0;
const intervals = new Map();
const rafCallbacks = [];

global.setInterval = (callback, ms) => {
  const id = ++sequence;
  intervals.set(id, { ms: Number(ms) || 0, callback });
  return id;
};
global.clearInterval = (id) => { intervals.delete(id); };
global.setTimeout = (callback) => { callback(); return ++sequence; };
global.clearTimeout = () => {};

function fireInterval(ms) {
  for (const timer of [...intervals.values()]) {
    if (timer.ms === ms) timer.callback();
  }
}

function flushRaf() {
  const callbacks = rafCallbacks.splice(0);
  for (const callback of callbacks) callback();
}

/* --------------------------------------------------------------- 假 DOM */

let byId = new Map();
const allElements = [];

class FakeClassList {
  constructor(owner) {
    this.owner = owner;
  }
  _classes() {
    return String(this.owner.className || "").split(/\s+/).filter(Boolean);
  }
  contains(name) {
    return this._classes().includes(name);
  }
  add(name) {
    if (!this.contains(name)) {
      this.owner.className = (String(this.owner.className || "") + " " + name).trim();
    }
  }
  remove(name) {
    this.owner.className = this._classes()
      .filter((entry) => entry !== name)
      .join(" ");
  }
}

class FakeElement {
  constructor(tagName) {
    this.tagName = String(tagName || "").toUpperCase();
    this.listeners = new Map();
    this.style = {
      setProperty(property, value) {
        this[property] = value;
      },
    };
    this.children = [];
    this.textContent = "";
    this.attributes = {};
    this.dataset = {};
    this.parentNode = null;
    this.className = "";
    this.classList = new FakeClassList(this);
    this.value = "";
    this.min = "";
    this.max = "";
    this.disabled = false;
    this.hidden = false;
    this.options = [];
    allElements.push(this);
    Object.defineProperty(this, "id", {
      get() { return this._id; },
      set(value) {
        this._id = value;
        if (value) byId.set(value, this);
      },
    });
  }

  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(listener);
  }

  emit(type, init = {}) {
    const event = Object.assign(
      {
        type,
        target: this,
        cancelBubble: false,
        stopPropagation() { this.cancelBubble = true; },
      },
      init
    );
    for (const listener of this.listeners.get(type) || []) listener(event);
    return event;
  }

  appendChild(child) {
    child.parentNode = this;
    this.children.push(child);
    if (child._id) byId.set(child._id, child);
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name === "hidden") this.hidden = true;
  }

  getAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this.attributes, name)
      ? this.attributes[name]
      : null;
  }
}

const body = new FakeElement("body");
body.id = "body";
const documentElement = body;

global.document = {
  readyState: "complete",
  body,
  documentElement,
  getElementById: (id) => byId.get(id) || null,
  createElement: (tagName) => new FakeElement(tagName),
  addEventListener: () => {},
  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  },
  querySelectorAll(selector) {
    const trimmed = selector.trim();
    const ariaMatch = trimmed.match(/^\[aria-controls="([^"]+)"\]$/);
    if (ariaMatch) {
      return allElements.filter(
        (el) => el.getAttribute("aria-controls") === ariaMatch[1]
      );
    }
    const found = [];
    const seen = new Set();
    const push = (el) => {
      if (!seen.has(el)) {
        seen.add(el);
        found.push(el);
      }
    };
    for (const part of trimmed.split(",")) {
      const selectorPart = part.trim();
      if (selectorPart === "button.player-gamepad-button") {
        for (const el of allElements) {
          if (el.tagName === "BUTTON" &&
              String(el.className || "").split(/\s+/).includes("player-gamepad-button")) {
            push(el);
          }
        }
      } else if (selectorPart === 'button[aria-label="Toggle Touch Gamepad"]') {
        for (const el of allElements) {
          if (el.tagName === "BUTTON" &&
              el.getAttribute("aria-label") === "Toggle Touch Gamepad") {
            push(el);
          }
        }
      } else if (selectorPart === 'button[title="Toggle Touch Gamepad"]') {
        for (const el of allElements) {
          if (el.tagName === "BUTTON" &&
              el.getAttribute("title") === "Toggle Touch Gamepad") {
            push(el);
          }
        }
      }
    }
    return found;
  },
  getElementsByTagName(tagName) {
    if (String(tagName).toUpperCase() === "*") return allElements.slice();
    return allElements.filter(
      (el) => el.tagName === String(tagName).toUpperCase()
    );
  },
};

let observerCallback = null;
let observeArgs = null;
global.MutationObserver = class {
  constructor(callback) {
    this.callback = callback;
  }
  observe(target, options) {
    observerCallback = this.callback;
    observeArgs = { target, options };
  }
};

const store = new Map();
const posts = [];
const fakeWindow = new FakeElement("window");
fakeWindow.location = {
  origin: "https://wechat.example",
  href: "https://wechat.example/index.html?x=1",
  hash: "",
};
fakeWindow.localStorage = {
  getItem: (key) => (store.has(key) ? store.get(key) : null),
  setItem: (key, value) => store.set(key, String(value)),
};
fakeWindow.postMessage = (data, origin) => posts.push({ data, origin });
fakeWindow.setTimeout = (callback) => { callback(); return ++sequence; };
fakeWindow.requestAnimationFrame = (callback) => {
  rafCallbacks.push(callback);
  return ++sequence;
};
global.window = fakeWindow;
global.location = fakeWindow.location;

const prefix = fakeWindow.location.href.split("#")[0].replace(/[^a-zA-Z0-9.-_]/g, "_");
const key = (name) => prefix + "_" + name;

function makeControl(id, tagName = "BUTTON", value = "") {
  const el = new FakeElement(tagName);
  el.id = id;
  if (tagName === "SELECT") {
    el.options = [
      { value: "x264enc-striped" },
      { value: "cbr" },
      { value: "crf" },
      { value: "96" },
      { value: "120" },
      { value: "144" },
      { value: "168" },
      { value: "192" },
    ];
    el.value = value;
  }
  const row = new FakeElement("DIV");
  row.className = "dev-setting-item";
  const label = new FakeElement("LABEL");
  label.textContent = id;
  label.setAttribute("for", id);
  row.appendChild(label);
  row.appendChild(el);
  body.appendChild(row);
  return el;
}

function makeSection(ariaControls, title) {
  const section = new FakeElement("DIV");
  section.className = "sidebar-section";
  const header = new FakeElement("DIV");
  header.className = "sidebar-section-header";
  header.setAttribute("aria-controls", ariaControls);
  const heading = new FakeElement("H3");
  heading.textContent = title;
  header.appendChild(heading);
  section.appendChild(header);
  body.appendChild(section);
  return section;
}

function makeCrfSlider(id, min, max, value) {
  const slider = new FakeElement("INPUT");
  slider.id = id;
  slider.type = "range";
  slider.min = String(min);
  slider.max = String(max);
  slider.value = String(value);
  body.appendChild(slider);
  return slider;
}

function makeGamepadButton() {
  const button = new FakeElement("BUTTON");
  button.className = "player-gamepad-button";
  button.setAttribute("aria-label", "Toggle Touch Gamepad");
  button.setAttribute("title", "Toggle Touch Gamepad");
  body.appendChild(button);
  return button;
}

function resetDom() {
  byId = new Map();
  allElements.length = 0;
  body.children.length = 0;
  store.clear();
  posts.length = 0;
  intervals.clear();
  observerCallback = null;
  observeArgs = null;
  rafCallbacks.length = 0;
  delete fakeWindow.wechatLockedSettingsInstalled;
  delete fakeWindow.wechatLockedSettingsObserver;
  fakeWindow.location.hash = "";
}

const scriptPath = process.argv[2] ||
  path.join(__dirname, "wechat-locked-settings.js");
const source = fs.readFileSync(scriptPath, "utf8");
const run = () => vm.runInThisContext(source, { filename: scriptPath });

/* 0. 只读/玩家页面不处理 ------------------------------------------------ */

resetDom();
fakeWindow.location.hash = "#shared";
run();
assert.equal(store.size, 0, "#shared writes nothing");
assert.equal(fakeWindow.wechatLockedSettingsInstalled, undefined);

/* 0.5 player 页面只隐藏浮动手柄按钮，不写设置 --------------------------- */

resetDom();
fakeWindow.location.hash = "#player2";
const playerButton = makeGamepadButton();
run();
fireInterval(500);
assert.equal(store.size, 0, "player page does not seed settings");
assert.equal(posts.length, 0, "player page does not post settings");
assert.equal(playerButton.style.display, "none", "floating gamepad button is hidden");
const latePlayerButton = makeGamepadButton();
observerCallback([]);
flushRaf();
assert.equal(latePlayerButton.style.display, "none", "late gamepad button is hidden");

/* 1. 首次加载写入十个键并隐藏已渲染设置行 -------------------------------- */

resetDom();
makeControl("hidpiToggle");
makeControl("forceAlignedResolutionToggle");
makeControl("antiAliasingToggle");
makeControl("useBrowserCursorsToggle");
makeControl("usePaintOverQualityToggle");
makeControl("h264StreamingModeToggle");
makeControl("useCpuToggle");
makeControl("encoderSelect", "SELECT");
makeControl("encoderRTCSelect", "SELECT");
makeControl("rateControlSelect", "SELECT");
makeControl("uiScalingSelect", "SELECT");
const appsSection = makeSection("apps-content", "应用程序");
const sharingSection = makeSection("sharing-content", "共享");
const screenSettingsSection = makeSection("screen-settings-content", "屏幕设置");
const videoSettingsSection = makeSection("video-settings-content", "视频设置");
const gamepadButton = makeGamepadButton();
const otherActionButton = makeControl("audioActionButton");
const videoCrf = makeCrfSlider("videoCRFSlider", 5, 50, 25);
const paintoverCrf = makeCrfSlider("h264PaintoverCRFSlider", 5, 50, 18);

run();

assert.equal(store.get(key("use_css_scaling")), "false");
assert.equal(store.get(key("useCssScaling")), "false");
assert.equal(store.get(key("force_aligned_resolution")), "true");
assert.equal(store.get(key("antiAliasingEnabled")), "true");
assert.equal(store.get(key("use_browser_cursors")), "true");
assert.equal(store.get(key("scaling_dpi")), "192");
assert.equal(store.get(key("encoder")), "x264enc-striped");
assert.equal(store.get(key("rate_control_mode")), "cbr");
assert.equal(store.get(key("use_paint_over_quality")), "true");
assert.equal(store.get(key("h264_streaming_mode")), "false");
assert.equal(store.get(key("use_cpu")), "false");
assert.equal(store.has(key("framerate")), false, "preset-owned keys are untouched");
assert.equal(store.has(key("video_bitrate")), false, "preset-owned keys are untouched");

fireInterval(500);

for (const id of [
  "hidpiToggle",
  "forceAlignedResolutionToggle",
  "antiAliasingToggle",
  "useBrowserCursorsToggle",
  "usePaintOverQualityToggle",
  "h264StreamingModeToggle",
  "useCpuToggle",
  "encoderSelect",
  "encoderRTCSelect",
  "rateControlSelect",
  "uiScalingSelect",
]) {
  const el = byId.get(id);
  assert.ok(el, `${id} exists`);
  const row = el.parentNode;
  assert.ok(row, `${id} has a parent row`);
  assert.equal(row.className, "dev-setting-item", `${id} row is a dev-setting-item`);
  assert.equal(row.style.display, "none", `${id} row is hidden`);
  assert.equal(row.getAttribute("hidden"), "", `${id} row is marked hidden`);
  assert.notEqual(row.children.length, 1, `${id} row hides label plus control`);
}

assert.equal(appsSection.style.display, "none", "Applications card is hidden");
assert.equal(appsSection.getAttribute("hidden"), "", "Applications is hidden");
assert.equal(sharingSection.style.display, "none", "Sharing card is hidden");
assert.equal(screenSettingsSection.style.display, "none", "Screen Settings card is hidden");
assert.equal(screenSettingsSection.getAttribute("hidden"), "", "Screen Settings is hidden");
assert.notEqual(videoSettingsSection.style.display, "none", "Video Settings card is not hidden");
assert.equal(gamepadButton.style.display, "none", "floating gamepad button is hidden");
assert.notEqual(otherActionButton.style.display, "none", "other action button is not hidden");
assert.deepEqual(observeArgs.options, { childList: true, subtree: true });

/* 2. CRF 滑块保留真实值，用 RTL 让最左差、最右好 ------------------------ */

assert.equal(videoCrf.style.direction, "rtl");
assert.equal(paintoverCrf.style.direction, "rtl");
assert.equal(videoCrf.value, "25", "CRF value is not remapped");
assert.equal(paintoverCrf.value, "18", "CRF value is not remapped");
assert.equal(fakeWindow.wechatLockedSettingsMapCrf, undefined);
assert.equal(videoCrf.listeners.has("input"), false, "no input interception");
assert.equal(paintoverCrf.listeners.has("input"), false, "no input interception");

/* 3. 通过侧边栏同款事件实时发送设置 -------------------------------------- */

const settingsPost = posts.find(
  (post) => post.data.type === "settings" && post.data.settings.scaling_dpi === 192
);
assert.ok(settingsPost, "settings message is posted");
assert.equal(settingsPost.data.settings.encoder, "x264enc-striped");
assert.equal(settingsPost.data.settings.rate_control_mode, "cbr");
assert.equal(settingsPost.data.settings.use_css_scaling, false);
assert.equal(settingsPost.data.settings.force_aligned_resolution, true);
assert.equal(settingsPost.data.settings.use_browser_cursors, true);
assert.equal(settingsPost.data.settings.use_paint_over_quality, true);
assert.equal(settingsPost.data.settings.h264_streaming_mode, false);
assert.equal(settingsPost.data.settings.use_cpu, false);
assert.ok(
  posts.some((post) => post.data.type === "setAntiAliasing" && post.data.value === true),
  "anti-aliasing has its own postMessage event"
);

/* 4. MutationObserver 隐藏后渲染的面板和设置行 --------------------------- */

body.children = body.children.filter(
  (child) =>
    child !== appsSection &&
    child !== sharingSection &&
    child !== screenSettingsSection
);
allElements.length = 0;
(function collect(node) {
  allElements.push(node);
  for (const child of node.children) collect(child);
})(body);
const lateApps = makeSection("apps-content", "Apps");
const lateScreenSettings = makeSection("screen-settings-content", "屏幕设置");
const lateToggle = makeControl("antiAliasingToggle");
const lateGamepadButton = makeGamepadButton();
assert.notEqual(lateApps.style.display, "none", "not hidden before the observer runs");
assert.notEqual(lateToggle.parentNode.style.display, "none", "row not hidden before the observer runs");
assert.ok(observerCallback, "MutationObserver is installed");
observerCallback([]);
observerCallback([]);
assert.equal(rafCallbacks.length, 1, "mutations in one frame are coalesced");
assert.notEqual(lateApps.style.display, "none", "not applied until the frame flush");
flushRaf();
assert.equal(lateApps.style.display, "none", "late Apps card is hidden");
assert.equal(lateScreenSettings.style.display, "none", "late Screen Settings card is hidden");
assert.equal(lateGamepadButton.style.display, "none", "late gamepad button is hidden");
assert.notEqual(videoSettingsSection.style.display, "none", "Video Settings is not hidden after mutations");
assert.notEqual(otherActionButton.style.display, "none", "other action button is not hidden after mutations");
assert.equal(lateToggle.parentNode.style.display, "none", "late toggle row is hidden");

/* 5. DOM mutation 不触发 seed，定时 enforce 才兜底回写 ------------------- */

store.set(key("use_cpu"), "true");
observerCallback([]);
flushRaf();
assert.equal(store.get(key("use_cpu")), "true", "DOM mutation does not re-seed");
fireInterval(1000);
assert.equal(store.get(key("use_cpu")), "false", "locked value is re-seeded");

/* 6. 重复安装不产生重复副作用 -------------------------------------------- */

const postsBeforeReload = posts.length;
run();
fireInterval(500);
assert.equal(posts.length, postsBeforeReload, "no second live post");
assert.equal(
  allElements.filter((el) => el.id === "wechat-topbar").length,
  0,
  "this script creates no UI"
);

console.log("wechat-locked-settings DOM tests passed");
