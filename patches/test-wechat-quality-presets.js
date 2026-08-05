#!/usr/bin/env node
"use strict";

// DOM-level tests for patches/wechat-quality-presets.js, in the same node:vm
// style as test-wechat-ime-anchor.js.
//
// What matters here is that the bar writes the keys Selkies actually reads
// (per-URL prefix included) *and* posts the message the bundle actually
// listens for. Getting either half wrong produces a control that looks like it
// works and changes nothing.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

/* ------------------------------------------------------------ fake timers */

let sequence = 0;
const intervals = new Map();
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

/* --------------------------------------------------------------- fake DOM */

let byId = new Map();

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
  setAttribute(name, value) { this.attributes[name] = value; }
}

let body = new FakeElement("body");

global.document = {
  readyState: "complete",
  get body() { return body; },
  getElementById: (id) => byId.get(id) || null,
  createElement: (tagName) => new FakeElement(tagName),
  addEventListener: () => {},
};

const store = new Map();
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
const posts = [];
fakeWindow.postMessage = (data, origin) => posts.push({ data, origin });
global.window = fakeWindow;
global.location = fakeWindow.location;

// Exactly the expression the bundle uses. The character class is a RANGE
// ".-_" (0x2E-0x5F), so ":" and "/" survive it — recomputed here rather than
// hardcoded so the test fails loudly if the script "corrects" the regex.
const prefix = fakeWindow.location.href.split("#")[0].replace(/[^a-zA-Z0-9.-_]/g, "_");
const key = (name) => prefix + "_" + name;

/* -------------------------------------------------------------- run script */

const scriptPath = process.argv[2] ||
  path.join(__dirname, "wechat-quality-presets.js");
const source = fs.readFileSync(scriptPath, "utf8");
const run = () => vm.runInThisContext(source, { filename: scriptPath });

/* 0. read-only viewers get no controls ------------------------------------- */

fakeWindow.location.hash = "#player2";
run();
assert.equal(store.size, 0, "#player installs nothing and writes nothing");
assert.equal(fakeWindow.wechatQualityPresetsInstalled, undefined);

/* 1. first load applies the 标清 default ------------------------------------ */

fakeWindow.location.hash = "";
run();

// Seeding happens synchronously at load, before the deferred bundle boots.
assert.equal(store.get("wechatQualityPreset"), "sd");
assert.equal(store.get(key("framerate")), "24");
assert.equal(store.get(key("video_bitrate")), "8");
assert.equal(store.get(key("encoder")), "x264enc-striped");
assert.equal(store.get(key("rate_control_mode")), "cbr");
assert.equal(posts.length, 0, "no settings message before the user asks for one");
assert.ok(prefix.indexOf("https://wechat.example/index.html") === 0, prefix);

fireInterval(500);   // boot poll finds document.body and builds the bar

const group = byId.get("wechat-quality-presets");
assert.ok(group, "preset bar was created");
const topbar = byId.get("wechat-topbar");
assert.ok(topbar, "shared top bar host was created");
assert.equal(group.parentNode, topbar);
assert.equal(topbar.parentNode, body);

const buttons = group.children.filter((child) => child.tagName === "BUTTON");
assert.deepEqual(
  buttons.map((button) => button.textContent),
  ["流畅", "标清", "高清", "超清"]
);
assert.deepEqual(
  buttons.map((button) => button.title),
  ["15 fps · 4 Mbps", "24 fps · 8 Mbps", "45 fps · 12 Mbps", "90 fps · 24 Mbps"]
);
assert.equal(
  group.children[group.children.length - 1].textContent,
  "更多设置见左侧边栏"
);
assert.deepEqual(
  buttons.map((button) => button.attributes["aria-pressed"]),
  ["false", "true", "false", "false"],
  "标清 is selected by default"
);

/* 2. clicking a preset writes the keys and posts the settings message ------ */

buttons[2].emit("click");
assert.equal(store.get("wechatQualityPreset"), "hd");
assert.equal(store.get(key("framerate")), "45");
assert.equal(store.get(key("video_bitrate")), "12");
assert.equal(store.get(key("encoder")), "x264enc-striped");
assert.equal(store.get(key("rate_control_mode")), "cbr");
assert.equal(posts.length, 1);
assert.equal(posts[0].origin, "https://wechat.example");
assert.deepEqual(posts[0].data, {
  type: "settings",
  settings: {
    encoder: "x264enc-striped",
    framerate: 45,
    rate_control_mode: "cbr",
    video_bitrate: 12,
  },
});
assert.deepEqual(
  buttons.map((button) => button.attributes["aria-pressed"]),
  ["false", "false", "true", "false"]
);
assert.equal(buttons[2].style.background, "#07c160");
assert.equal(buttons[1].style.background, "transparent");

/* 3. installing twice in one page does nothing ----------------------------- */

run();
fireInterval(500);
assert.equal(topbar.children.length, 1, "no duplicate preset bar");
assert.equal(posts.length, 1);

/* 4. the choice survives a reload ------------------------------------------ */

delete fakeWindow.wechatQualityPresetsInstalled;
byId = new Map();
body = new FakeElement("body");
intervals.clear();
run();
assert.equal(store.get(key("framerate")), "45", "the stored preset is re-seeded");
fireInterval(500);
const reloadedButtons = byId
  .get("wechat-quality-presets")
  .children.filter((child) => child.tagName === "BUTTON");
assert.deepEqual(
  reloadedButtons.map((button) => button.attributes["aria-pressed"]),
  ["false", "false", "true", "false"],
  "高清 is still selected after a reload"
);

/* 5. a corrupt stored value falls back to the default ---------------------- */

store.set("wechatQualityPreset", "ultra-hyper");
delete fakeWindow.wechatQualityPresetsInstalled;
byId = new Map();
body = new FakeElement("body");
intervals.clear();
run();
assert.equal(store.get("wechatQualityPreset"), "sd");
assert.equal(store.get(key("framerate")), "24");

console.log("wechat-quality-presets DOM tests passed");
