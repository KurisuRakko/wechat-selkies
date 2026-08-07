#!/usr/bin/env node
"use strict";

// DOM-level tests for patches/wechat-desktop-export.js, in the same node:vm
// style as test-wechat-quality-presets.js.
//
// The two things worth guarding are the ones a screenshot would not catch: the
// preset bar must come back on every path out of a drag (drag-end, timeout,
// stream error), and the drop zone must be drawn on the rectangle the helper
// actually accepts drops on — a hint that is merely near it downloads nothing.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

/* ------------------------------------------------------------ fake timers */

let sequence = 0;
const timeouts = new Map();
global.setTimeout = (callback, ms) => {
  const id = ++sequence;
  timeouts.set(id, { ms: Number(ms) || 0, callback });
  return id;
};
global.clearTimeout = (id) => { timeouts.delete(id); };

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
    this.clicks = 0;
    Object.defineProperty(this, "id", {
      get() { return this._id; },
      set(value) { this._id = value; byId.set(value, this); },
    });
  }

  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(listener);
  }

  appendChild(child) { child.parentNode = this; this.children.push(child); }
  removeChild(child) {
    this.children = this.children.filter((node) => node !== child);
    child.parentNode = null;
  }
  setAttribute(name, value) { this.attributes[name] = value; }
  click() { this.clicks += 1; downloads.push({ href: this.href, name: this.download }); }
}

let body = new FakeElement("body");
let downloads = [];

global.document = {
  readyState: "complete",
  get body() { return body; },
  documentElement: { clientWidth: 1616 },
  getElementById: (id) => byId.get(id) || null,
  createElement: (tagName) => new FakeElement(tagName),
  addEventListener: () => {},
};

/* --------------------------------------------------------- fake EventSource */

let sources = [];

class FakeEventSource {
  constructor(url) {
    this.url = url;
    this.listeners = new Map();
    sources.push(this);
  }
  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(listener);
  }
  emit(type, data) {
    const event = { type, data: data === undefined ? undefined : JSON.stringify(data) };
    for (const listener of this.listeners.get(type) || []) listener(event);
  }
  emitRaw(type, data) {
    for (const listener of this.listeners.get(type) || []) listener({ type, data });
  }
}

const fakeWindow = new FakeElement("window");
fakeWindow.location = {
  origin: "https://wechat.example",
  href: "https://wechat.example/index.html",
  pathname: "/index.html",
  hash: "",
};
fakeWindow.innerWidth = 1616;
fakeWindow.EventSource = FakeEventSource;
global.window = fakeWindow;
global.location = fakeWindow.location;
global.EventSource = FakeEventSource;

/* -------------------------------------------------------------- run script */

const scriptPath = process.argv[2] ||
  path.join(__dirname, "wechat-desktop-export.js");
const source = fs.readFileSync(scriptPath, "utf8");
const run = () => vm.runInThisContext(source, { filename: scriptPath });

const warnings = [];
const originalWarn = console.warn;
console.warn = (...args) => { warnings.push(args.join(" ")); };
console.log = () => {};

function reset({ withPresets = true } = {}) {
  delete fakeWindow.wechatDesktopExportInstalled;
  byId = new Map();
  body = new FakeElement("body");
  downloads = [];
  sources = [];
  timeouts.clear();
  warnings.length = 0;
  fakeWindow.location.hash = "";
  fakeWindow.location.pathname = "/index.html";
  fakeWindow.innerWidth = 1616;
  if (withPresets) {
    const group = new FakeElement("div");
    group.id = "wechat-quality-presets";
    group.style.display = "flex";
  }
}

const presets = () => byId.get("wechat-quality-presets");
const zone = () => byId.get("wechat-export-dropzone");

// A 3232x2048 remote screen against a 1616 CSS viewport is the deployment's
// real geometry: devicePixelRatio 2, so every remote number halves.
const DRAG = {
  zone: { x: 2596, y: 16, w: 620, h: 300 },
  screen: { w: 3232, h: 2048 },
};

/* 0. read-only viewers install nothing --------------------------------------- */

reset();
fakeWindow.location.hash = "#player2";
run();
assert.equal(sources.length, 0, "#player opens no event stream");
assert.equal(fakeWindow.wechatDesktopExportInstalled, undefined);

fakeWindow.location.hash = "#shared";
run();
assert.equal(sources.length, 0, "#shared opens no event stream");

/* 1. the stream is opened relative to the page ------------------------------- */

reset();
run();
assert.equal(sources.length, 1);
assert.equal(sources[0].url, "/wechat-export/events");

reset();
fakeWindow.location.pathname = "/sub/folder/index.html";
run();
assert.equal(sources[0].url, "/sub/folder/wechat-export/events",
  "SUBFOLDER deployments reach their own nginx location");

/* 2. drag-start hides the preset bar and draws the zone on the reported rect -- */

reset();
run();
const stream = sources[0];
assert.equal(presets().style.display, "flex");

stream.emit("drag-start", DRAG);
assert.equal(presets().style.display, "none", "preset bar is hidden");
const dropZone = zone();
assert.ok(dropZone, "drop zone was created");
assert.equal(dropZone.parentNode, body);
assert.equal(dropZone.style.display, "flex");
// Remote device pixels / (3232 / 1616) = CSS pixels.
assert.equal(dropZone.style.left, "1298px");
assert.equal(dropZone.style.top, "8px");
assert.equal(dropZone.style.width, "310px");
assert.equal(dropZone.style.height, "150px");
assert.ok(/拖到这里下载/.test(
  dropZone.children.map((child) => child.textContent).join("")),
  "zone carries the drop hint");
assert.ok(/pointer-events:none/.test(dropZone.style.cssText),
  "the zone must never swallow a pointer event meant for the stream");

/* 3. drag-end restores the bar and hides the zone ---------------------------- */

stream.emit("drag-end", { reason: "released" });
assert.equal(presets().style.display, "flex", "preset bar is back");
assert.equal(zone().style.display, "none");

/* 4. a drag with no drag-end restores on the timeout ------------------------- */

stream.emit("drag-start", DRAG);
assert.equal(presets().style.display, "none");
fireTimeout(10000);
assert.equal(presets().style.display, "flex", "timeout restores the preset bar");
assert.equal(zone().style.display, "none");
assert.ok(warnings.some((line) => /no drag-end within 10000ms/.test(line)));

/* 5. a stream error mid-drag also restores ----------------------------------- */

stream.emit("drag-start", DRAG);
assert.equal(presets().style.display, "none");
stream.emitRaw("error", undefined);
assert.equal(presets().style.display, "flex", "a dropped stream cannot hide the bar forever");

/* 6. a tab that connects mid-drag draws the zone from hello ------------------ */

reset();
run();
sources[0].emit("hello", { dragging: true, zone: DRAG.zone, screen: DRAG.screen });
assert.equal(presets().style.display, "none");
assert.equal(zone().style.display, "flex");
sources[0].emit("hello", { dragging: false, zone: null, screen: null });
assert.equal(presets().style.display, "flex");

/* 7. file-exported triggers one download per file ---------------------------- */

reset();
run();
sources[0].emit("file-exported", { name: "报告.docx", url: "wechat-export/file/abc123" });

async function drain() {
  for (let i = 0; i < 8; i += 1) {
    await new Promise((resolve) => setImmediate(resolve));
    fireTimeout(350);
  }
}

async function main() {
  await drain();
  assert.equal(downloads.length, 1);
  assert.deepEqual(downloads[0], {
    href: "/wechat-export/file/abc123",
    name: "报告.docx",
  });
  assert.equal(body.children.length, 0, "the download anchor is removed again");

  /* 8. several files in one drop each download -------------------------------- */

  reset();
  fakeWindow.location.pathname = "/sub/index.html";
  run();
  sources[0].emit("file-exported", { name: "a.png", url: "wechat-export/file/t1" });
  sources[0].emit("file-exported", { name: "b.png", url: "wechat-export/file/t2" });
  await drain();
  assert.deepEqual(downloads.map((entry) => entry.href), [
    "/sub/wechat-export/file/t1",
    "/sub/wechat-export/file/t2",
  ]);

  /* 9. a file-exported with no url is logged, not thrown ---------------------- */

  reset();
  run();
  sources[0].emit("file-exported", { name: "x" });
  await drain();
  assert.equal(downloads.length, 0);
  assert.ok(warnings.some((line) => /file-exported without a url/.test(line)));

  /* 10. a missing preset bar degrades to a warning ---------------------------- */

  reset({ withPresets: false });
  run();
  sources[0].emit("drag-start", DRAG);
  assert.ok(zone(), "the zone still appears without the preset bar");
  assert.equal(zone().style.display, "flex");
  assert.ok(warnings.some((line) => /#wechat-quality-presets not found/.test(line)));
  sources[0].emit("drag-end", {});
  assert.equal(zone().style.display, "none");

  /* 11. no EventSource at all is a log, not a throw --------------------------- */

  reset();
  delete fakeWindow.EventSource;
  run();
  assert.equal(sources.length, 0);
  assert.ok(warnings.some((line) => /EventSource unavailable/.test(line)));
  fakeWindow.EventSource = FakeEventSource;

  /* 12. unscaled viewports (devicePixelRatio 1) place the zone 1:1 ------------ */

  reset();
  fakeWindow.innerWidth = 1280;
  run();
  sources[0].emit("drag-start", {
    zone: { x: 954, y: 6, w: 320, h: 140 },
    screen: { w: 1280, h: 800 },
  });
  assert.equal(zone().style.left, "954px");
  assert.equal(zone().style.width, "320px");

  /* 13. a nonsense screen width falls back to 1:1 instead of NaN ------------- */

  reset();
  run();
  sources[0].emit("drag-start", { zone: { x: 10, y: 5, w: 30, h: 20 }, screen: { w: 0, h: 0 } });
  assert.equal(zone().style.left, "10px");
  assert.equal(zone().style.width, "30px");

  /* 14. a malformed payload is logged, not thrown ---------------------------- */

  reset();
  run();
  sources[0].emitRaw("drag-start", "{not json");
  assert.ok(warnings.some((line) => /bad event payload/.test(line)));
  assert.equal(zone(), undefined, "no zone from a broken payload");

  /* 15. installing twice in one page opens one stream ------------------------ */

  reset();
  run();
  run();
  assert.equal(sources.length, 1, "second load is a no-op");
}

main().then(() => {
  console.warn = originalWarn;
  process.stdout.write("wechat-desktop-export DOM tests passed\n");
}).catch((error) => {
  console.warn = originalWarn;
  originalWarn(error);
  process.exit(1);
});
