#!/usr/bin/env node
"use strict";

// DOM-level tests for patches/wechat-connection-status.js, in the same node:vm
// style as test-wechat-ime-anchor.js.
//
// The watchdog is the part worth pinning down: it reloads the page, so every
// reason NOT to reload (hidden tab, upload in flight, too many reloads already)
// has to hold, and the freshness it decides on must come from the local clock
// rather than the server's timestamp string.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

/* ------------------------------------------------------------ fake timers */

let clock = 0;          // performance.now()
let wallClock = 1_700_000_000_000;   // Date.now()
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

// Snapshot first: install() registers another 500 ms interval while the boot
// interval is being drained.
function fireInterval(ms) {
  for (const timer of [...intervals.values()]) {
    if (timer.ms === ms) timer.callback();
  }
}

global.performance = { now: () => clock };
Date.now = () => wallClock;

/* --------------------------------------------------------------- fake DOM */

const byId = new Map();

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
  }
  setAttribute(name, value) { this.attributes[name] = value; }
}

const body = new FakeElement("body");
const head = new FakeElement("head");

global.document = {
  readyState: "complete",
  hidden: false,
  body,
  head,
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

const session = new Map();
let reloads = 0;

const fakeWindow = new FakeElement("window");
fakeWindow.location = {
  origin: "https://wechat.example",
  href: "https://wechat.example/",
  hash: "",
  reload: () => { reloads += 1; },
};
fakeWindow.performance = global.performance;
fakeWindow.navigator = { onLine: true };
fakeWindow.sessionStorage = {
  getItem: (key) => (session.has(key) ? session.get(key) : null),
  setItem: (key, value) => session.set(key, String(value)),
};
global.window = fakeWindow;
global.location = fakeWindow.location;

/* -------------------------------------------------------------- run script */

const scriptPath = process.argv[2] ||
  path.join(__dirname, "wechat-connection-status.js");
const source = fs.readFileSync(scriptPath, "utf8");
const run = () => vm.runInThisContext(source, { filename: scriptPath });

/* 0. a read-only viewer gets no pill at all ------------------------------- */

fakeWindow.location.hash = "#shared";
run();
assert.equal(byId.has("wechat-connection-pill"), false, "#shared installs nothing");
assert.equal(fakeWindow.wechatConnectionStatusInstalled, undefined);

fakeWindow.location.hash = "";
run();
fireInterval(500);   // boot poll finds document.body and installs

const pill = byId.get("wechat-connection-pill");
assert.ok(pill, "pill was created");
const topbar = byId.get("wechat-topbar");
assert.ok(topbar, "shared top bar host was created");
assert.equal(pill.parentNode, topbar);
assert.equal(topbar.parentNode, body);
assert.equal(topbar.style.cssText.indexOf("position:fixed"), 0);

const style = byId.get("wechat-topbar-style");
assert.ok(style, "notification-container offset style was injected");
assert.match(style.textContent, /\.notification-container\{top:64px !important;\}/);

/* helpers ------------------------------------------------------------------ */

const dot = () => pill.children[0];
const label = () => pill.children[1];
const state = () => [dot().style.background, label().textContent];

function at(ms) {
  clock = ms;
  fireInterval(500);
}

/* 1. idempotent ------------------------------------------------------------ */

run();
fireInterval(500);
assert.equal(topbar.children.length, 1, "no duplicate pill");
assert.equal(body.children.filter((c) => c._id === "wechat-topbar").length, 1);
assert.equal((fakeWindow.listeners.get("online") || []).length, 1);
assert.equal((document.listeners.get("visibilitychange") || []).length, 1);

/* 2. before any stats ------------------------------------------------------ */

assert.deepEqual(state(), ["#d29922", "连接中…"]);

/* 3. fresh stats are green and show latency and bandwidth ------------------ */

fakeWindow.network_stats = {
  type: "network_stats",
  timestamp: "2026-08-05T10:00:00.000",
  latency_ms: 42.4,
  bandwidth_mbps: 3.25,
};
at(1000);
assert.deepEqual(state(), ["#3fb950", "42ms ↓3.3Mbps"]);
assert.ok(
  !label().textContent.includes("fps"),
  "fps is deliberately not shown; a static screen legitimately encodes 0"
);

/* 4. offline is immediate, not aged ---------------------------------------- */

fakeWindow.navigator.onLine = false;
fakeWindow.emit("offline");
assert.deepEqual(state(), ["#f85149", "网络离线"]);
fakeWindow.navigator.onLine = true;
fakeWindow.emit("online");
assert.deepEqual(state(), ["#3fb950", "42ms ↓3.3Mbps"]);

/* 5. a repeated timestamp does not count as a refresh ---------------------- */

at(8000);
assert.deepEqual(state(), ["#d29922", "网络卡顿"]);

/* 6. an actually new timestamp does ---------------------------------------- */

fakeWindow.network_stats = {
  timestamp: "2026-08-05T10:00:08.000",
  latency_ms: 12,
  bandwidth_mbps: 24,
};
at(8500);
assert.deepEqual(state(), ["#3fb950", "12ms ↓24Mbps"]);

/* 7. the staleness ladder, and no reload before 20 s ----------------------- */

at(24000);   // age 15.5 s
assert.deepEqual(state(), ["#f85149", "连接中断"]);
assert.equal(reloads, 0);
assert.equal(session.size, 0, "nothing recorded before a reload is attempted");

/* 8. a hidden tab is throttled, so it never triggers a reload -------------- */

document.hidden = true;
at(40000);   // age 31.5 s
assert.equal(reloads, 0, "a hidden tab is not reloaded");
document.hidden = false;

/* 9. returning to the foreground gets a grace period ----------------------- */

document.emit("visibilitychange");
assert.deepEqual(state(), ["#d29922", "网络卡顿"], "grace period, not a reload");
assert.equal(reloads, 0);

/* 10. an upload in flight suppresses the watchdog -------------------------- */

fakeWindow.emit("message", {
  origin: "https://wechat.example",
  data: { type: "fileUpload", payload: { status: "progress", fileName: "big.zip" } },
});
at(50000);   // age 41.5 s, but the upload was 10 s ago
assert.equal(reloads, 0, "an upload in flight suppresses the reload");
// A message from elsewhere must not be able to suppress it.
fakeWindow.emit("message", {
  origin: "https://attacker.example",
  data: { type: "fileUpload" },
});

/* 11. three reloads in ten minutes and it stops asking --------------------- */

session.set(
  "wechatConnStatus.reloads",
  JSON.stringify([wallClock - 1000, wallClock - 2000, wallClock - 3000])
);
at(70000);   // upload grace expired
assert.equal(reloads, 0, "the fourth reload in ten minutes is refused");
assert.deepEqual(state(), ["#f85149", "连接已断开，请手动刷新"]);

/* 12. reloads older than the window do not count --------------------------- */

session.set(
  "wechatConnStatus.reloads",
  JSON.stringify([wallClock - 700000, wallClock - 800000, wallClock - 900000])
);
at(80000);
assert.deepEqual(state(), ["#f85149", "连接已卡死，正在刷新…"]);
assert.equal(reloads, 0, "the reload is deferred, not immediate");
clock = 80600;
runDueTimeouts();
assert.equal(reloads, 1, "reloaded after the announcement");
assert.deepEqual(
  JSON.parse(session.get("wechatConnStatus.reloads")),
  [wallClock],
  "expired entries are pruned and this reload is recorded"
);

/* 13. the latch means one reload per page load ----------------------------- */

at(120000);
runDueTimeouts();
assert.equal(reloads, 1, "the reload latch holds");

console.log("wechat-connection-status DOM tests passed");
