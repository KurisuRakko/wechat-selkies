#!/usr/bin/env node
"use strict";

// DOM-level tests for patches/wechat-second-display.js, in the same node:vm
// style as test-wechat-quality-presets.js / test-wechat-connection-status.js.
//
// What matters here: the #display2 passive-mode guard actually short-circuits
// before anything else runs, the prompt bar tracks unassigned_count from the
// status endpoint, and the click handler calls window.open with a URL/name
// pair that lets a second click reuse the same window instead of opening a
// new one — and does NOT pass noopener/noreferrer, which would silently make
// window.open() return null in a real browser and break that reuse.

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

// Fires the single most-recently-registered timeout at this delay (that is
// what a real single-flight `pollTimer = setTimeout(tick, pollDelay)` chain
// produces: at most one pending timeout per delay value at a time).
function fireTimeout(ms) {
  for (const [id, timer] of [...timeouts.entries()]) {
    if (timer.ms === ms) {
      timeouts.delete(id);
      timer.callback();
      return true;
    }
  }
  return false;
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

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

  appendChild(child) { child.parentNode = this; this.children.push(child); return child; }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  removeChild(child) {
    const index = this.children.indexOf(child);
    if (index >= 0) this.children.splice(index, 1);
    // A real document.getElementById only finds elements still attached to
    // the tree; keep the fake byId map in sync so "was it actually removed"
    // can be asserted the same way the script itself checks for one.
    if (child.id !== undefined) byId.delete(child.id);
    child.parentNode = null;
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  querySelector(selector) {
    // Only the one selector pattern the script actually uses.
    if (selector === "button[data-action]") {
      return this.children.find((c) => c.attributes["data-action"]) || null;
    }
    return null;
  }
}

let body = new FakeElement("body");

global.document = {
  readyState: "complete",
  get body() { return body; },
  getElementById: (id) => byId.get(id) || null,
  createElement: (tagName) => new FakeElement(tagName),
  addEventListener: () => {},
};

const fakeWindow = new FakeElement("window");
fakeWindow.location = {
  origin: "https://wechat.example",
  href: "https://wechat.example/index.html",
  hash: "",
};

let fetchCalls = [];
let fetchBehavior = () => Promise.resolve({ ok: true, json: () => Promise.resolve({ unassigned_count: 0 }) });
fakeWindow.fetch = (url, options) => {
  fetchCalls.push({ url: String(url), options });
  return fetchBehavior();
};

let openCalls = [];
let openReturnValue = null;
fakeWindow.open = (url, name, features) => {
  openCalls.push({ url, name, features });
  return openReturnValue;
};

global.window = fakeWindow;
global.location = fakeWindow.location;
global.fetch = fakeWindow.fetch;

/* -------------------------------------------------------------- run script */

const scriptPath = process.argv[2] ||
  path.join(__dirname, "wechat-second-display.js");
const source = fs.readFileSync(scriptPath, "utf8");
const run = () => vm.runInThisContext(source, { filename: scriptPath });

function reset() {
  delete fakeWindow.wechatSecondDisplayInstalled;
  byId = new Map();
  body = new FakeElement("body");
  intervals.clear();
  timeouts.clear();
  fetchCalls = [];
  openCalls = [];
  openReturnValue = null;
  fakeWindow.location.hash = "";
  fetchBehavior = () => Promise.resolve({ ok: true, json: () => Promise.resolve({ unassigned_count: 0 }) });
}

function bootAndFetch() {
  run();
  fireInterval(500); // boot poll finds document.body, calls tick() once
  return settle();
}

function respondWith(unassignedCount) {
  fetchBehavior = () => Promise.resolve({
    ok: true,
    json: () => Promise.resolve({ version: 1, displays: [], movable_count: unassignedCount, unassigned_count: unassignedCount }),
  });
}

async function main() {

/* 0. read-only viewers and the secondary display itself install nothing --- */

for (const hash of ["#shared", "#player2", "#display2", "#display2-right"]) {
  reset();
  fakeWindow.location.hash = hash;
  run();
  assert.equal(fakeWindow.wechatSecondDisplayInstalled, undefined, hash);
  assert.equal(fetchCalls.length, 0, hash + " must not poll status");
}

/* 1. normal load polls the status endpoint ---------------------------------- */

reset();
respondWith(0);
await bootAndFetch();
assert.equal(fetchCalls.length, 1);
assert.match(fetchCalls[0].url, /wechat-second-display\/api\/status$/);
assert.equal(fetchCalls[0].options.cache, "no-store");
assert.equal(byId.has("wechat-second-display-prompt"), false, "no prompt while unassigned_count is 0");

/* 2. unassigned_count > 0 renders the prompt bar ---------------------------- */

reset();
respondWith(3);
await bootAndFetch();
const prompt = byId.get("wechat-second-display-prompt");
assert.ok(prompt, "prompt bar was created");
assert.equal(prompt.parentNode, body);
const actionButton = prompt.querySelector("button[data-action]");
assert.ok(actionButton, "prompt has an action button");
assert.match(actionButton.textContent, /3 个可弹出的微信窗口/);
const dismissButton = prompt.children[1];
assert.equal(dismissButton.textContent, "✕");

/* 3. clicking the action opens a named, reusable popup ---------------------- */

openReturnValue = { closed: false, focus() { this.focused = (this.focused || 0) + 1; } };
actionButton.emit("click");
assert.equal(openCalls.length, 1);
assert.equal(openCalls[0].url, "https://wechat.example/index.html#display2");
assert.equal(openCalls[0].name, "wechat-second-display");
assert.equal(openCalls[0].features, undefined, "noopener/noreferrer must not be passed (see file header)");
assert.equal(byId.has("wechat-second-display-prompt"), false, "prompt hides immediately on a successful open");

/* 4. a second click reuses/focuses the same window instead of opening another */

actionButton.emit("click");
assert.equal(openCalls.length, 1, "no second window.open call");
assert.equal(openReturnValue.focused, 1, "the existing handle was focused");

/* 5. the ✕ dismiss button removes the bar without opening anything ---------- */

reset();
respondWith(2);
await bootAndFetch();
const prompt2 = byId.get("wechat-second-display-prompt");
const dismiss2 = prompt2.children[1];
dismiss2.emit("click");
assert.equal(byId.has("wechat-second-display-prompt"), false);
assert.equal(openCalls.length, 0);

/* 6. window.open being blocked falls back to a real anchor ------------------ */

reset();
respondWith(1);
await bootAndFetch();
openReturnValue = null;
byId.get("wechat-second-display-prompt").querySelector("button[data-action]").emit("click");
const fallback = byId.get("wechat-second-display-prompt");
assert.ok(fallback, "fallback prompt replaces the original one");
const link = fallback.children[0];
assert.equal(link.tagName, "A");
assert.equal(link.href, "https://wechat.example/index.html#display2");
assert.equal(link.target, "_blank");

/* 7. a poll landing while the fallback prompt is showing must not throw ---- */

// showPrompt() used to blindly call
// existing.querySelector("button[data-action]").textContent = ... — but the
// fallback bar (still occupying the same PROMPT_ID) has an <a>, not a
// button[data-action], so that threw a TypeError inside the fetch .then
// handler. The chain's own .catch then mistook it for a network failure and
// started backing off, and the fallback bar's count never updated again.
reset();
respondWith(1);
await bootAndFetch();
openReturnValue = null; // simulate the popup blocker
byId.get("wechat-second-display-prompt").querySelector("button[data-action]").emit("click");
const fallbackShowing = byId.get("wechat-second-display-prompt");
assert.equal(fallbackShowing.children[0].tagName, "A", "fallback link is showing");

respondWith(5);
assert.ok(fireTimeout(5000), "next scheduled poll fires while the fallback is up");
await settle();
assert.equal(fetchCalls.length, 2, "the poll after the fallback still went through (no exception, no backoff)");
const rebuilt = byId.get("wechat-second-display-prompt");
assert.ok(rebuilt, "a prompt is still showing after the poll");
const rebuiltButton = rebuilt.querySelector("button[data-action]");
assert.ok(rebuiltButton, "the normal action button replaced the fallback link");
assert.match(rebuiltButton.textContent, /5 个可弹出的微信窗口/);
assert.ok(fireTimeout(5000), "polling resumed at the normal 5s interval, not a backed-off one");

/* 8. closing the secondary window brings the prompt back within 2s --------- */

reset();
respondWith(4);
await bootAndFetch();
const handle = { closed: false, focus() {} };
openReturnValue = handle;
byId.get("wechat-second-display-prompt").querySelector("button[data-action]").emit("click");
assert.equal(byId.has("wechat-second-display-prompt"), false);
handle.closed = true;
fireInterval(2000);
assert.ok(byId.get("wechat-second-display-prompt"), "prompt reappears once the handle is observed closed");

/* 9. failures back off exponentially to a 30s ceiling; success resets to 5s */

reset();
fetchBehavior = () => Promise.reject(new Error("network down"));
await bootAndFetch();                          // call 1 fails
assert.equal(fetchCalls.length, 1);
assert.equal(fireTimeout(5000), false, "must not reschedule at the original 5s delay");
assert.ok(fireTimeout(10000), "call 1's failure doubles 5000 -> 10000");   // launches call 2

await settle();                                // call 2 fails
assert.equal(fetchCalls.length, 2);
assert.equal(fireTimeout(10000), false, "must not reschedule at 10s again");
assert.ok(fireTimeout(20000), "call 2's failure doubles 10000 -> 20000");  // launches call 3

await settle();                                // call 3 fails
assert.equal(fetchCalls.length, 3);
assert.equal(fireTimeout(20000), false);
// call 3's failure would double 20000 -> 40000, clamped to the 30s ceiling.
// Flip to a succeeding response BEFORE launching call 4, since firing the
// timer below invokes tick() (and therefore fetch()) synchronously.
respondWith(0);
assert.ok(fireTimeout(30000), "clamps to the 30s ceiling");                // launches call 4

await settle();                                // call 4 succeeds
assert.equal(fetchCalls.length, 4);
assert.ok(fireTimeout(5000), "a successful poll resets the interval back to 5s");
assert.equal(fireTimeout(30000), false, "no leftover 30s timer once the interval has reset");

}

main().then(() => {
  console.log("wechat-second-display DOM tests passed");
}).catch((error) => {
  console.error(error);
  process.exit(1);
});
