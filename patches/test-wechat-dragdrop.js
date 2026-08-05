#!/usr/bin/env node
"use strict";

// DOM-level tests for patches/wechat-dragdrop.js, in the same node:vm style as
// test-wechat-ime-anchor.js. The guarantees under test are the ones that cost
// a whole session when they regress:
//
//   * a dropped image is NEVER pushed through the stream socket as one
//     `cb,image/png,` message (that is what used to trip the server's 1 MiB
//     max_size and close the connection with 1009);
//   * a finished upload calls the server-side attach endpoint with the right
//     body, and only falls back to a blind Ctrl+V when that endpoint is not
//     reachable at all — not when the server refused for a stated reason;
//   * folder members and failed uploads never trigger either path;
//   * the pending table cannot leak entries forever.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

/* ------------------------------------------------------------ fake timers */

let clock = 0;
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

function advance(ms) {
  clock += ms;
  for (let guard = 0; guard < 100; guard += 1) {
    const due = [...timeouts.entries()]
      .filter(([, timer]) => timer.at <= clock)
      .sort((a, b) => a[1].at - b[1].at);
    if (!due.length) return;
    for (const [id, timer] of due) {
      timeouts.delete(id);
      timer.callback();
    }
  }
  throw new Error("timer queue did not settle");
}

function fireInterval(ms) {
  for (const timer of intervals.values()) {
    if (timer.ms === ms) timer.callback();
  }
}

const flush = async () => {
  for (let i = 0; i < 40; i += 1) await new Promise((r) => setImmediate(r));
};

let wallClock = 1_700_000_000_000;
Date.now = () => wallClock;

/* --------------------------------------------------------------- fake DOM */

class FakeElement {
  constructor(tagName) {
    this.tagName = String(tagName || "").toUpperCase();
    this.listeners = new Map();
    this.dataset = {};
    this.style = {};
    this.children = [];
    this.textContent = "";
    this.parentNode = null;
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
    child.parentNode = null;
  }

  setAttribute(name, value) { this[name] = value; }
  getAttribute(name) { return this[name] === undefined ? null : this[name]; }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
}

const overlay = new FakeElement("input");
const body = new FakeElement("body");

global.document = {
  readyState: "complete",
  hidden: false,
  body,
  documentElement: new FakeElement("html"),
  getElementById: (id) => (id === "overlayInput" ? overlay : null),
  createElement: (tagName) => new FakeElement(tagName),
  querySelectorAll: () => [],
  addEventListener: () => {},
};

global.MutationObserver = class {
  constructor(callback) { this.callback = callback; }
  observe() {}
};

const storage = new Map();
global.localStorage = {
  getItem: (key) => (storage.has(key) ? storage.get(key) : null),
  setItem: (key, value) => storage.set(key, String(value)),
};

const fakeWindow = new FakeElement("window");
fakeWindow.location = { origin: "https://wechat.example", pathname: "/", hash: "" };
const sent = [];
fakeWindow.webrtcInput = { send: (message) => sent.push(message) };
// Not 0: the script reads `Number(window.X || 500)`, so a falsy value keeps
// the 500 ms default. Kept explicit so the paste delay is visible here.
fakeWindow.WECHAT_DRAGDROP_PASTE_DELAY = 500;
fakeWindow.WECHAT_ATTACH_TIMEOUT_MS = 55000;
global.window = fakeWindow;
global.location = fakeWindow.location;
global.localStorage = global.localStorage;

/* ------------------------------------------------------------ fake network */

const fetches = [];
let nextFetch = () => Promise.resolve({ ok: true, status: 200 });
global.fetch = (url, options) => {
  if (String(url).indexOf("wechat-open-urls") !== -1) {
    return Promise.resolve({ ok: false, status: 404, text: () => Promise.resolve("") });
  }
  fetches.push({ url: String(url), options });
  return nextFetch();
};

/* -------------------------------------------------------------- run script */

const scriptPath = process.argv[2] || path.join(__dirname, "wechat-dragdrop.js");
vm.runInThisContext(fs.readFileSync(scriptPath, "utf8"), { filename: scriptPath });

// boot() polls on a 500 ms interval until #overlayInput and webrtcInput exist.
fireInterval(500);
assert.ok(overlay.dataset.wechatDragdrop, "attached to #overlayInput");
assert.equal((fakeWindow.listeners.get("message") || []).length, 1);

/* ------------------------------------------------------------------ helpers */

function drop(files) {
  overlay.emit("drop", { dataTransfer: { files } });
}

function upload(payload) {
  fakeWindow.emit("message", {
    origin: "https://wechat.example",
    data: { type: "fileUpload", payload },
  });
}

function toasts() {
  return body.children.map((child) => child.textContent);
}

function lastToast() {
  const all = toasts();
  return all.length ? all[all.length - 1] : null;
}

async function main() {
  /* 1. an image drop must never produce a cb,image/png stream message ------ */

  drop([{ name: "screenshot.png", size: 4_000_000, type: "image/png" }]);
  await flush();
  advance(1000);
  await flush();
  assert.deepEqual(sent, [], "dropping an image sent nothing over the stream");

  upload({ status: "start", fileName: "screenshot.png", fileSize: 4_000_000 });
  upload({ status: "progress", fileName: "screenshot.png", progress: 50 });
  await flush();
  assert.equal(fetches.length, 0, "attach is not requested before the upload ends");

  upload({ status: "end", fileName: "screenshot.png", fileSize: 4_000_000 });
  await flush();
  assert.equal(fetches.length, 1);
  assert.match(fetches[0].url, /^\/wechat-notifications\/api\/attach$/);
  assert.equal(fetches[0].options.method, "POST");
  assert.equal(fetches[0].options.headers["Content-Type"], "application/json");
  assert.deepEqual(JSON.parse(fetches[0].options.body), {
    fileName: "screenshot.png",
    size: 4_000_000,
    kind: "image",
  });
  assert.match(lastToast(), /已放入微信聊天框: screenshot\.png/);
  assert.ok(
    !sent.some((message) => String(message).indexOf("cb,image/") === 0),
    "no clipboard image message was ever sent"
  );

  /* 2. an unreachable endpoint degrades to the blind uri-list paste -------- */

  sent.length = 0;
  nextFetch = () => Promise.reject(new Error("connection refused"));
  drop([{ name: "notes.txt", size: 11, type: "text/plain" }]);
  upload({ status: "end", fileName: "notes.txt", fileSize: 11 });
  await flush();
  assert.equal(fetches.length, 2);
  assert.deepEqual(JSON.parse(fetches[1].options.body), {
    fileName: "notes.txt",
    size: 11,
    kind: "file",
  });
  assert.equal(sent.length, 1, "uri-list clipboard message was sent");
  assert.match(sent[0], /^cb,text\/uri-list,/);
  const uri = Buffer.from(sent[0].split(",").slice(2).join(","), "base64").toString("utf8");
  assert.equal(uri, "file:///config/Desktop/notes.txt\r\n");

  advance(600);
  await flush();
  assert.deepEqual(
    sent.slice(1),
    ["kd,65507", "kd,118", "ku,118", "ku,65507"],
    "Ctrl+V followed the clipboard write"
  );

  /* 3. a stated server refusal must NOT fall back to a blind paste --------- */

  sent.length = 0;
  nextFetch = () => Promise.resolve({
    ok: false,
    status: 409,
    json: () => Promise.resolve({
      ok: false,
      error: { code: "REPLY_BUSY", message: "另一个草稿操作正在进行" },
    }),
  });
  drop([{ name: "busy.bin", size: 5, type: "" }]);
  upload({ status: "end", fileName: "busy.bin", fileSize: 5 });
  await flush();
  assert.equal(fetches.length, 3);
  assert.deepEqual(sent, [], "a 409 does not trigger the fallback paste");
  assert.match(lastToast(), /未能放入聊天框.*另一个草稿操作正在进行/);

  /* 4. folder members are announced once and never attached ---------------- */

  sent.length = 0;
  nextFetch = () => Promise.resolve({ ok: true, status: 200 });
  drop([{ name: "album", size: 0, type: "" }]);
  upload({ status: "end", fileName: "album/one.png", fileSize: 10 });
  upload({ status: "end", fileName: "album/two.png", fileSize: 10 });
  upload({ status: "end", fileName: "album/nested/three.png", fileSize: 10 });
  await flush();
  assert.equal(fetches.length, 3, "folder members are not attached");
  assert.deepEqual(sent, []);
  assert.equal(
    toasts().filter((text) => /文件夹已上传到 Desktop\/album/.test(text)).length,
    1,
    "one toast per folder, not per member"
  );
  // The folder's own entry is dropped, so a later same-named file is not
  // silently attached with stale metadata.
  upload({ status: "end", fileName: "album", fileSize: 0 });
  await flush();
  assert.equal(fetches.length, 3);

  /* 5. a failed upload attaches nothing ------------------------------------ */

  drop([{ name: "broken.zip", size: 99, type: "application/zip" }]);
  upload({ status: "error", fileName: "broken.zip", message: "WS closed" });
  await flush();
  assert.equal(fetches.length, 3);
  assert.deepEqual(sent, []);
  // The entry was removed, so a late "end" cannot resurrect it.
  upload({ status: "end", fileName: "broken.zip", fileSize: 99 });
  await flush();
  assert.equal(fetches.length, 3);

  /* 6. the pending table is swept -------------------------------------------- */

  drop([{ name: "forgotten.dat", size: 7, type: "" }]);
  wallClock += 200000;
  fireInterval(30000);
  upload({ status: "end", fileName: "forgotten.dat", fileSize: 7 });
  await flush();
  assert.equal(fetches.length, 3, "a swept entry is not attached");

  /* 7. a message from another origin is ignored ------------------------------ */

  drop([{ name: "evil.txt", size: 1, type: "" }]);
  fakeWindow.emit("message", {
    origin: "https://attacker.example",
    data: { type: "fileUpload", payload: { status: "end", fileName: "evil.txt" } },
  });
  await flush();
  assert.equal(fetches.length, 3, "cross-origin upload messages are ignored");

  /* 8. toasts clean themselves up -------------------------------------------- */

  const before = body.children.length;
  assert.ok(before > 0);
  advance(6000);
  assert.equal(body.children.length, 0, "toasts removed themselves");

  console.log("wechat-dragdrop DOM tests passed");
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
