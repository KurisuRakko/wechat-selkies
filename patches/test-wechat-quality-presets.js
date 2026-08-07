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
const timeouts = new Map();
global.setInterval = (callback, ms) => {
  const id = ++sequence;
  intervals.set(id, { ms: Number(ms) || 0, callback });
  return id;
};
global.clearInterval = (id) => { intervals.delete(id); };
global.setTimeout = (callback, ms) => {
  const id = ++sequence;
  timeouts.set(id, { ms: Number(ms) || 0, callback });
  return id;
};
global.clearTimeout = (id) => { timeouts.delete(id); };

function fireInterval(ms) {
  for (const timer of [...intervals.values()]) {
    if (timer.ms === ms) timer.callback();
  }
}

function fireTimeout(ms) {
  for (const [id, timer] of [...timeouts.entries()]) {
    if (timer.ms === ms) {
      timeouts.delete(id);
      timer.callback();
    }
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
  pathname: "/index.html",
  hash: "",
};
fakeWindow.localStorage = {
  getItem: (key) => (store.has(key) ? store.get(key) : null),
  setItem: (key, value) => store.set(key, String(value)),
};
const posts = [];
fakeWindow.postMessage = (data, origin) => posts.push({ data, origin });
let nowMs = 0;
let nextDownloadBytes = 600000;
let nextDownloadMs = 1000;
let nextRttMs = 20;
let fetchCalls = [];
fakeWindow.performance = { now: () => nowMs };
fakeWindow.fetch = (url, options = {}) => {
  fetchCalls.push({ url: String(url), options });
  nowMs += options.method === "HEAD" ? nextRttMs : nextDownloadMs;
  return Promise.resolve({
    ok: true,
    headers: { get: () => null },
    arrayBuffer: () => Promise.resolve(new Uint8Array(nextDownloadBytes).buffer),
  });
};
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

function resetQualityState() {
  delete fakeWindow.wechatQualityPresetsInstalled;
  byId = new Map();
  body = new FakeElement("body");
  intervals.clear();
  timeouts.clear();
  fetchCalls.length = 0;
  posts.length = 0;
  nowMs = 0;
  nextDownloadBytes = 600000;
  nextDownloadMs = 1000;
  nextRttMs = 20;
  fakeWindow.location.hash = "";
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

async function main() {

/* 0. read-only viewers get no controls ------------------------------------- */

fakeWindow.location.hash = "#player2";
run();
assert.equal(store.size, 0, "#player installs nothing and writes nothing");
assert.equal(fakeWindow.wechatQualityPresetsInstalled, undefined);

/* 1. first load applies the 流畅 default ----------------------------------- */

fakeWindow.location.hash = "";
run();

// Seeding happens synchronously at load, before the deferred bundle boots.
assert.equal(store.get("wechatQualityPreset"), "smooth");
assert.equal(store.get(key("framerate")), "24");
assert.equal(store.get(key("video_bitrate")), "6");
assert.equal(store.get(key("h264_paintover_crf")), "28");
assert.equal(store.get(key("encoder")), "x264enc-striped");
assert.equal(store.get(key("rate_control_mode")), "cbr");
assert.equal(posts.length, 0, "speed test is async and does not block seeding");
assert.ok(prefix.indexOf("https://wechat.example/index.html") === 0, prefix);

await settle();
assert.equal(store.get("wechatQualityPreset"), "smooth", "auto speed test keeps smooth");
assert.equal(posts.length, 1, "auto speed test posts its selected preset");
assert.deepEqual(posts[0].data.settings, {
  encoder: "x264enc-striped",
  framerate: 24,
  rate_control_mode: "cbr",
  video_bitrate: 6,
  h264_paintover_crf: 28,
});
assert.ok(
  fetchCalls.some((call) => /wechat-speedtest\.bin\?cachebust=/.test(call.url)),
  "speed test uses the dedicated 1 MiB payload"
);
assert.ok(
  fetchCalls.every((call) => call.options.cache === "no-store"),
  "all speed test requests bypass the cache"
);
assert.ok(
  fetchCalls.some(
    (call) => call.options.signal && typeof call.options.signal.aborted === "boolean"
  ),
  "download request carries an AbortController signal"
);
posts.length = 0;

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
  ["省流", "流畅", "高清", "极致"]
);
assert.deepEqual(
  buttons.map((button) => button.title),
  [
    "12 fps · 2 Mbps · 静态 CRF 33",
    "24 fps · 6 Mbps · 静态 CRF 28",
    "30 fps · 12 Mbps · 静态 CRF 23",
    "60 fps · 20 Mbps · 静态 CRF 18"
  ]
);
assert.equal(
  group.children.length,
  buttons.length,
  "preset bar contains only the buttons"
);
assert.deepEqual(
  buttons.map((button) => button.attributes["aria-pressed"]),
  ["false", "true", "false", "false"],
  "流畅 is selected by default"
);

/* 2. clicking a preset writes the keys and posts the settings message ------ */

buttons[2].emit("click");
assert.equal(store.get("wechatQualityPreset"), "hd");
assert.equal(store.get(key("framerate")), "30");
assert.equal(store.get(key("video_bitrate")), "12");
assert.equal(store.get(key("h264_paintover_crf")), "23");
assert.equal(store.get(key("encoder")), "x264enc-striped");
assert.equal(store.get(key("rate_control_mode")), "cbr");
assert.equal(posts.length, 1);
assert.equal(posts[0].origin, "https://wechat.example");
assert.deepEqual(posts[0].data, {
  type: "settings",
  settings: {
    encoder: "x264enc-striped",
    framerate: 30,
    rate_control_mode: "cbr",
    video_bitrate: 12,
    h264_paintover_crf: 23,
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
assert.equal(fetchCalls.length, 2, "re-install does not start another speed test");

/* 4. reload keeps the old preset only until the speed test finishes --------- */

resetQualityState();
run();
assert.equal(store.get(key("framerate")), "30", "stored preset is the temporary value");
assert.equal(store.get(key("h264_paintover_crf")), "23");
await settle();
assert.equal(store.get("wechatQualityPreset"), "smooth", "speed test overrides the stored preset");
assert.equal(store.get(key("framerate")), "24");
assert.equal(store.get(key("h264_paintover_crf")), "28");
fireInterval(500);
const reloadedButtons = byId
  .get("wechat-quality-presets")
  .children.filter((child) => child.tagName === "BUTTON");
assert.deepEqual(
  reloadedButtons.map((button) => button.attributes["aria-pressed"]),
  ["false", "true", "false", "false"],
  "流畅 is selected after the reload speed test"
);

/* 5. removed preset ids fall back to the default --------------------------- */

store.set("wechatQualityPreset", "sd");
resetQualityState();
run();
await settle();
assert.equal(store.get("wechatQualityPreset"), "smooth");
assert.equal(store.get(key("framerate")), "24");
assert.equal(store.get(key("h264_paintover_crf")), "28");

store.set("wechatQualityPreset", "uhd");
resetQualityState();
run();
await settle();
assert.equal(store.get("wechatQualityPreset"), "smooth");
assert.equal(store.get(key("framerate")), "24");
assert.equal(store.get(key("h264_paintover_crf")), "28");

/* 6. auto speed test maps bandwidth to a preset ---------------------------- */

resetQualityState();
nextDownloadBytes = 1400000;
nextDownloadMs = 1000;
nextRttMs = 20;
run();
await settle();
assert.equal(store.get("wechatQualityPreset"), "hd", "11.2 Mbps selects 高清");
assert.equal(store.get(key("framerate")), "30");
assert.equal(store.get(key("h264_paintover_crf")), "23");
assert.ok(
  fetchCalls.some((call) => call.options.cache === "no-store"),
  "speed test requests use cache:no-store"
);

resetQualityState();
nextDownloadBytes = 2000000;
nextDownloadMs = 1000;
nextRttMs = 20;
run();
await settle();
assert.equal(store.get("wechatQualityPreset"), "max", "16 Mbps selects 极致");

/* 7. high RTT caps auto selection at 流畅 ----------------------------------- */

resetQualityState();
nextDownloadBytes = 2000000;
nextDownloadMs = 1000;
nextRttMs = 200;
run();
await settle();
assert.equal(store.get("wechatQualityPreset"), "smooth", "RTT > 150ms caps at 流畅");

resetQualityState();
nextDownloadBytes = 200000;
nextDownloadMs = 1000;
nextRttMs = 200;
run();
await settle();
assert.equal(store.get("wechatQualityPreset"), "datasaver", "RTT cap never upgrades 省流");

/* 8. a manual click locks the session against auto changes ----------------- */

resetQualityState();
nextDownloadBytes = 1400000;
nextDownloadMs = 1000;
nextRttMs = 20;
run();
fireInterval(500);
const lockedButtons = byId
  .get("wechat-quality-presets")
  .children.filter((child) => child.tagName === "BUTTON");
lockedButtons[3].emit("click");
await settle();
assert.equal(store.get("wechatQualityPreset"), "max", "manual choice wins");
assert.equal(store.get(key("framerate")), "60");

/* 9. an auto result arriving after the bar exists still updates highlight --- */

resetQualityState();
store.set("wechatQualityPreset", "smooth");
nextDownloadBytes = 2000000;
nextDownloadMs = 1000;
nextRttMs = 20;
let resolveDownload;
fakeWindow.fetch = (url, options = {}) => {
  fetchCalls.push({ url: String(url), options });
  if (options.method === "HEAD") {
    nowMs += nextRttMs;
    return Promise.resolve({
      ok: true,
      headers: { get: () => null },
      arrayBuffer: () => Promise.resolve(new ArrayBuffer(0)),
    });
  }
  return new Promise((resolve) => {
    resolveDownload = () => {
      nowMs += nextDownloadMs;
      resolve({
        ok: true,
        headers: { get: () => null },
        arrayBuffer: () => Promise.resolve(new Uint8Array(nextDownloadBytes).buffer),
      });
    };
  });
};
run();
fireInterval(500);
await settle();
const delayedButtons = byId
  .get("wechat-quality-presets")
  .children.filter((child) => child.tagName === "BUTTON");
assert.equal(delayedButtons[3].attributes["aria-pressed"], "false");
resolveDownload();
await settle();
assert.equal(store.get("wechatQualityPreset"), "max");
assert.equal(delayedButtons[3].attributes["aria-pressed"], "true");

/* 10. timeout keeps the current preset ------------------------------------- */

resetQualityState();
store.set("wechatQualityPreset", "smooth");
let timeoutSignal;
fakeWindow.fetch = (url, options = {}) => {
  if (options.signal) timeoutSignal = options.signal;
  return new Promise(() => {});
};
run();
await settle();
assert.equal(store.get("wechatQualityPreset"), "smooth", "before timeout");
assert.ok(timeoutSignal, "timeout test captured the abort signal");
fireTimeout(3000);
await settle();
assert.equal(store.get("wechatQualityPreset"), "smooth", "timeout keeps current preset");
assert.equal(timeoutSignal.aborted, true, "timeout aborts the download");

/* 11. a synchronous fetch throw still keeps the current preset -------------- */

resetQualityState();
store.set("wechatQualityPreset", "smooth");
fakeWindow.fetch = () => {
  throw new Error("sync fetch failure");
};
run();
await settle();
assert.equal(store.get("wechatQualityPreset"), "smooth");

}

main().then(() => {
  console.log("wechat-quality-presets DOM tests passed");
}).catch((error) => {
  console.error(error);
  process.exit(1);
});
