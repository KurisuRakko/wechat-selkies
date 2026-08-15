#!/usr/bin/env node
"use strict";

// DOM 级测试 for patches/wechat-idle-saver.js，node:vm 风格。
// 融合 test-wechat-connection-status.js 的假定时器/假 DOM（document.hidden
// 可变、事件可派发、clock 可推进）与 test-wechat-quality-presets.js 的假
// localStorage(store)/假 postMessage(posts) 断言习惯。
// 覆盖全部 15 个场景：入口守卫、默认状态、60 秒挂起、快照精确还原、
// hidden/focus/blur 语义、开关三态、幂等、自愈、localStorage 异常。

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

/* ------------------------------------------------------------ fake timers */

let clock = 0;                          // performance.now()
let wallClock = 1_700_000_000_000;      // Date.now()
let sequence = 0;
const timeouts = new Map();
const intervals = new Map();

global.setTimeout = (callback, ms) => {
  const id = ++sequence;
  timeouts.set(id, { at: clock + (Number(ms) || 0), callback });
  return id;
};
global.clearTimeout = (id) => { timeouts.delete(id); };
global.setInterval = (callback, ms) => {
  const id = ++sequence;
  intervals.set(id, { ms: Number(ms) || 0, callback });
  return id;
};
global.clearInterval = (id) => { intervals.delete(id); };

function runDueTimeouts() {
  const due = [...timeouts.entries()]
    .filter(([, timer]) => timer.at <= clock)
    .sort((a, b) => a[1].at - b[1].at);
  for (const [id, timer] of due) {
    timeouts.delete(id);
    timer.callback();
  }
}

// Snapshot first: install() registers another interval while the boot interval
// is being drained.
function fireInterval(ms) {
  for (const timer of [...intervals.values()]) {
    if (timer.ms === ms) timer.callback();
  }
}

global.performance = { now: () => clock };
Date.now = () => wallClock;

/* --------------------------------------------------------------- fake DOM */

class FakeElement {
  constructor(tagName) {
    this.tagName = String(tagName || "").toUpperCase();
    this.listeners = new Map();
    this.style = {};
    this.children = [];
    this.textContent = "";
    this.attributes = {};
    this.parentNode = null;
    Object.defineProperty(this, "id", {
      get() { return this._id; },
      set(value) { this._id = value; byId.set(value, this); },
    });
  }

  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(listener);
  }

  emit(type, init = {}) {
    const event = Object.assign({ type, target: this }, init);
    for (const listener of this.listeners.get(type) || []) listener(event);
    return event;
  }

  appendChild(child) { child.parentNode = this; this.children.push(child); }
  removeChild(child) {
    const index = this.children.indexOf(child);
    if (index >= 0) this.children.splice(index, 1);
    // 与真实 DOM 一致：被移除的节点不再能被 getElementById 找到。
    if (child._id) byId.delete(child._id);
  }
  setAttribute(name, value) { this.attributes[name] = value; }
}

let byId = new Map();
let body = new FakeElement("body");

const document = {
  readyState: "complete",
  hidden: false,
  get body() { return body; },
  listeners: new Map(),
  getElementById: (id) => byId.get(id) || null,
  createElement: (tagName) => new FakeElement(tagName),
  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(listener);
  },
  emit(type) {
    for (const listener of this.listeners.get(type) || []) listener({ type });
  },
};

const store = new Map();
const posts = [];
const fakeWindow = new FakeElement("window");
fakeWindow.location = {
  origin: "https://wechat.example",
  href: "https://wechat.example/",
  hash: "",
};
fakeWindow.localStorage = {
  getItem: (key) => (store.has(key) ? store.get(key) : null),
  setItem: (key, value) => store.set(key, String(value)),
};
fakeWindow.postMessage = (data, origin) => posts.push({ data, origin });
fakeWindow.performance = global.performance;
global.window = fakeWindow;
global.location = fakeWindow.location;
global.document = document;

// Exactly the expression the bundle uses. The character class is a RANGE
// ".-_" (0x2E-0x5F), so ":" and "/" survive it — recomputed here rather than
// hardcoded so the test fails loudly if the script "corrects" the regex.
const prefix = fakeWindow.location.href.split("#")[0].replace(/[^a-zA-Z0-9.-_]/g, "_");
const key = (name) => prefix + "_" + name;

/* -------------------------------------------------------------- run script */

const scriptPath = process.argv[2] ||
  path.join(__dirname, "wechat-idle-saver.js");
const source = fs.readFileSync(scriptPath, "utf8");
const run = () => vm.runInThisContext(source, { filename: scriptPath });

function resetState() {
  delete fakeWindow.wechatIdleSaverInstalled;
  byId = new Map();
  body = new FakeElement("body");
  document.listeners.clear();
  fakeWindow.listeners.clear();
  intervals.clear();
  timeouts.clear();
  posts.length = 0;
  store.clear();
  clock = 0;
  document.hidden = false;
  fakeWindow.location.hash = "";
}

// run + 一次 boot 轮询，等 install() 完成。
const install = () => { run(); fireInterval(500); };

const button = () => byId.get("wechat-idle-saver-toggle");
const dot = () => button().children[0];
const label = () => button().children[1];
const state = () => [dot().style.background, label().textContent];
const storedValues = () =>
  [key("framerate"), key("video_bitrate"), key("h264_paintover_crf")]
    .map((k) => store.get(k));

/* 0. #shared/#player 只读视图不安装任何东西 ---------------------------------- */

fakeWindow.location.hash = "#shared";
run();
assert.equal(button(), undefined, "#shared installs no toggle");
assert.equal(fakeWindow.wechatIdleSaverInstalled, undefined);
assert.equal(intervals.size, 0, "#shared registers no timers");
assert.equal(document.listeners.size, 0, "#shared registers no listeners");

fakeWindow.location.hash = "#player";
run();
assert.equal(button(), undefined, "#player installs no toggle");
assert.equal(fakeWindow.wechatIdleSaverInstalled, undefined);
assert.equal(intervals.size, 0, "#player registers no timers");

/* 1. 默认安装：开启、GREEN、挂到共享 topbar 末尾 ----------------------------- */

fakeWindow.location.hash = "";
run();
fireInterval(500);   // boot 轮询发现 document.body，执行 install

const topbar = byId.get("wechat-topbar");
assert.ok(topbar, "shared top bar host was created");
assert.equal(topbar.parentNode, body);
assert.equal(
  body.children.filter((c) => c._id === "wechat-topbar").length,
  1,
  "topbar is not created twice"
);

const toggle = button();
assert.ok(toggle, "toggle button was created");
assert.equal(toggle.tagName, "BUTTON", "a real <button> element");
assert.equal(toggle.type, "button");
assert.equal(toggle.parentNode, topbar, "toggle hangs under the shared topbar");
assert.equal(topbar.children[topbar.children.length - 1], toggle, "toggle is appended last");
assert.equal(toggle.children.length, 2, "pill is dot + label");

assert.deepEqual(state(), ["#3fb950", "自动省流"], "default: enabled, green");
assert.equal(toggle.attributes["aria-pressed"], "true", "aria-pressed mirrors enabled");
assert.equal(document.listeners.get("visibilitychange").length, 1);
assert.equal(fakeWindow.listeners.get("focus").length, 1);
for (const type of ["mousemove", "mousedown", "keydown", "wheel", "touchstart"]) {
  assert.equal(document.listeners.get(type).length, 1, type + " listener installed once");
}
assert.equal(fakeWindow.listeners.get("blur"), undefined, "no blur listener at all");
assert.equal(
  [...intervals.values()].filter((t) => t.ms === 1000).length,
  1,
  "exactly one 1 s tick interval"
);

/* 2. 未到 60 秒：三个键不变、无消息 ------------------------------------------ */

clock = 59000;
fireInterval(1000);
assert.deepEqual(storedValues(), [undefined, undefined, undefined], "keys untouched before 60 s");
assert.equal(posts.length, 0, "no settings message before 60 s");
assert.deepEqual(state(), ["#3fb950", "自动省流"]);

/* 3. 到 60 秒且可见：挂起为省流档；用户保存的画质预设不被碰 -------------------- */

store.set("wechatQualityPreset", "smooth");   // 模拟用户手动保存的画质预设
clock = 60000;
fireInterval(1000);
assert.deepEqual(storedValues(), ["12", "2", "33"], "suspended to the 省流 values");
assert.equal(posts.length, 1);
assert.equal(posts[0].origin, "https://wechat.example");
assert.deepEqual(posts[0].data, {
  type: "settings",
  settings: { framerate: 12, video_bitrate: 2, h264_paintover_crf: 33 },
});
assert.deepEqual(
  Object.keys(posts[0].data.settings),
  ["framerate", "video_bitrate", "h264_paintover_crf"],
  "only the three keys are posted, never encoder/rate_control_mode"
);
assert.deepEqual(state(), ["#d29922", "省电中"]);
assert.equal(toggle.attributes["aria-pressed"], "true", "aria-pressed still reflects enabled");
assert.equal(store.get("wechatQualityPreset"), "smooth", "user preset untouched (core assertion)");

/* 4. 挂起前是高清档 30/12/23，mousemove/keydown 后精确还原 -------------------- */

resetState();
store.set("wechatQualityPreset", "hd");
store.set(key("framerate"), "30");
store.set(key("video_bitrate"), "12");
store.set(key("h264_paintover_crf"), "23");
install();
clock = 60000;
fireInterval(1000);
assert.deepEqual(storedValues(), ["12", "2", "33"], "suspended to savings values");

document.emit("mousemove");
assert.deepEqual(storedValues(), ["30", "12", "23"], "mousemove restores the exact snapshot");
assert.deepEqual(posts[1].data.settings, {
  framerate: 30, video_bitrate: 12, h264_paintover_crf: 23,
}, "restore message carries the numeric snapshot");
assert.deepEqual(state(), ["#3fb950", "自动省流"]);

clock = 120000;
fireInterval(1000);
assert.deepEqual(storedValues(), ["12", "2", "33"], "suspended again");
document.emit("keydown");
assert.deepEqual(storedValues(), ["30", "12", "23"], "keydown restores the exact snapshot");

/* 5. document.hidden=true + visibilitychange：立即挂起，不等 60 秒 ------------ */

resetState();
store.set(key("framerate"), "30");
store.set(key("video_bitrate"), "12");
store.set(key("h264_paintover_crf"), "23");
install();
clock = 5000;   // 远未到 60 秒
document.hidden = true;
document.emit("visibilitychange");
assert.deepEqual(storedValues(), ["12", "2", "33"], "hidden suspends immediately");
assert.deepEqual(state(), ["#d29922", "省电中"]);

/* 6. hidden=false + visibilitychange：立即恢复 -------------------------------- */

document.hidden = false;
document.emit("visibilitychange");
assert.deepEqual(storedValues(), ["30", "12", "23"], "becoming visible restores immediately");
assert.deepEqual(state(), ["#3fb950", "自动省流"]);

/* 7. window blur 不挂起；window focus 在挂起时立即恢复 ------------------------ */

fakeWindow.emit("blur");
assert.deepEqual(state(), ["#3fb950", "自动省流"], "blur itself never suspends");
assert.deepEqual(storedValues(), ["30", "12", "23"], "keys unchanged by blur");

clock = 70000;   // 自 5000 起已超 60 秒（blur 之后时间戳不再刷新）
fireInterval(1000);
assert.deepEqual(storedValues(), ["12", "2", "33"], "idle timeout still fires without input");
fakeWindow.emit("focus");
assert.deepEqual(storedValues(), ["30", "12", "23"], "focus resumes immediately");
assert.deepEqual(state(), ["#3fb950", "自动省流"]);

/* 8. 未挂起时点开关关闭：落盘 "0"，此后推进超 60 秒也不挂起 -------------------- */

resetState();
install();
clock = 1000;
button().emit("click");
assert.deepEqual(state(), ["#8b949e", "自动省流：关"], "off state is grey with 关 label");
assert.equal(store.get("wechatIdleSaverEnabled"), "0", "disabled persisted as 0");
assert.equal(button().attributes["aria-pressed"], "false");

clock = 70000;
fireInterval(1000);
assert.deepEqual(storedValues(), [undefined, undefined, undefined], "disabled never suspends");
assert.equal(posts.length, 0, "disabled never posts settings");

/* 9. 挂起时点开关关闭：先还原快照，再保持关闭 ---------------------------------- */

resetState();
store.set(key("framerate"), "30");
store.set(key("video_bitrate"), "12");
store.set(key("h264_paintover_crf"), "23");
install();
clock = 60000;
fireInterval(1000);
assert.deepEqual(state(), ["#d29922", "省电中"]);

button().emit("click");
assert.deepEqual(storedValues(), ["30", "12", "23"], "closing while suspended restores first");
assert.deepEqual(state(), ["#8b949e", "自动省流：关"]);
assert.equal(store.get("wechatIdleSaverEnabled"), "0");

clock = 120000;
fireInterval(1000);
assert.deepEqual(storedValues(), ["30", "12", "23"], "stays off and never suspends again");

/* 10. 再点开启：宽限期重新计时，不会立即挂起 ---------------------------------- */

button().emit("click");   // 此刻 clock 仍为 120000
assert.equal(store.get("wechatIdleSaverEnabled"), "1", "enabled persisted as 1");
assert.deepEqual(state(), ["#3fb950", "自动省流"]);

clock = 179999;   // 开启后 59999 ms
fireInterval(1000);
assert.deepEqual(
  storedValues(),
  ["30", "12", "23"],
  "fresh grace period: a full 60 s must elapse again"
);

clock = 180000;   // 开启后整整 60 秒
fireInterval(1000);
assert.deepEqual(storedValues(), ["12", "2", "33"], "after a full 60 s it suspends again");

/* 11. 两轮挂起/恢复用不同前值：快照不粘连 -------------------------------------- */

resetState();
store.set("wechatQualityPreset", "smooth");
store.set(key("framerate"), "24");
store.set(key("video_bitrate"), "6");
store.set(key("h264_paintover_crf"), "28");
install();
clock = 60000;
fireInterval(1000);          // A 轮挂起
document.emit("mousemove");  // A 轮恢复
assert.deepEqual(storedValues(), ["24", "6", "28"], "round A restores 流畅");

// 用户随后切到极致档（模拟点击画质预设后落盘的值）
store.set(key("framerate"), "60");
store.set(key("video_bitrate"), "20");
store.set(key("h264_paintover_crf"), "18");
clock = 120000;
fireInterval(1000);          // B 轮挂起
document.emit("keydown");    // B 轮恢复
assert.deepEqual(storedValues(), ["60", "20", "18"], "round B restores 极致, no sticky snapshot");

/* 12. 同一文档重复 run()：幂等 ------------------------------------------------ */

const currentTopbar = byId.get("wechat-topbar");
run();
fireInterval(500);
assert.equal(
  currentTopbar.children.filter((c) => c._id === "wechat-idle-saver-toggle").length,
  1,
  "no duplicate toggle button"
);
assert.equal(document.listeners.get("mousemove").length, 1, "listeners not duplicated");
assert.equal(fakeWindow.listeners.get("focus").length, 1);
assert.equal(
  [...intervals.values()].filter((t) => t.ms === 1000).length,
  1,
  "tick interval not duplicated"
);

/* 13. 自愈：按钮被移除后下一次 tick 重建，视觉状态与当前状态一致 ---------------- */

currentTopbar.removeChild(button());
assert.equal(button(), undefined, "button is really gone");

clock = 120000;   // 推进一次 tick
fireInterval(1000);
const healed = button();
assert.ok(healed, "toggle is rebuilt by the next tick");
assert.equal(healed.parentNode, currentTopbar);
assert.deepEqual(state(), ["#3fb950", "自动省流"], "rebuilt with the current enabled state");

// 挂起状态下被移除，也按「省电中」重建
currentTopbar.removeChild(button());
clock = 180000;   // 距上次活动正好 60 秒
fireInterval(1000);
assert.deepEqual(state(), ["#d29922", "省电中"], "rebuilt while suspended shows 省电中");

/* 14. localStorage 读写抛异常：不抛错、不落盘但仍 postMessage、不写 NaN --------- */

resetState();
fakeWindow.localStorage = {
  getItem: () => { throw new Error("storage denied"); },
  setItem: () => { throw new Error("storage denied"); },
};
run();            // readEnabled 兜底 true
fireInterval(500);
assert.deepEqual(
  state(),
  ["#3fb950", "自动省流"],
  "enabled defaults to true despite storage errors"
);

clock = 60000;
fireInterval(1000);   // suspend：读取快照失败、落盘失败，但 postMessage 仍发生
assert.equal(posts.length, 1, "suspend still posts despite storage errors");

document.emit("mousemove");   // resume：快照字段全 null → 只告警、不写入
assert.equal(posts.length, 1, "resume with an incomplete snapshot writes nothing (no NaN)");
assert.deepEqual(state(), ["#3fb950", "自动省流"], "resumed cleanly");

clock = 120000;
fireInterval(1000);   // 可再次挂起
assert.equal(posts.length, 2, "can suspend again");

console.log("wechat-idle-saver DOM tests passed");
