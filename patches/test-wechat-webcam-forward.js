#!/usr/bin/env node
"use strict";

// DOM-level tests for patches/wechat-webcam-forward.js, in the same node:vm
// style as test-wechat-quality-presets.js (fake timers/DOM) and
// test-wechat-dragdrop.js.
//
// The guarantees under test are the ones that cost the feature when they
// regress:
//
//   * the button never appears on read-only #shared/#player pages;
//   * one click requests the camera with the exact fixed 640x480@15fps
//     constraints, opens the WebSocket at the SUBFOLDER-safe ws(s):// URL,
//     and a timer tick ships exactly one raw JPEG frame (no type prefix
//     bytes) over that socket;
//   * a second click stops every track, closes the socket and resets
//     aria-pressed;
//   * a deployment under a URL prefix keeps that prefix in the socket URL.

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

/* --------------------------------------------------------------- fake DOM */

let byId = new Map();

class FakeElement {
  constructor(tagName) {
    this.tagName = String(tagName || "").toUpperCase();
    this.listeners = new Map();
    this.style = {};
    this.children = [];
    this.textContent = "";
    this.parentNode = null;
    this.attributes = {};
    if (this.tagName === "CANVAS") {
      this.getContext = () => fakeCtx;
      this.toBlob = (callback, type, quality) => {
        blobCalls.push({ type, quality });
        callback(fakeBlob);
      };
    }
    if (this.tagName === "VIDEO") {
      this.play = () => { playCalls += 1; };
    }
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
    child.parentNode = null;
  }

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

/* --------------------------------------------------------- fake media APIs */

// 假摄像头采集：getUserMedia 记录 constraints 并返回一条可 stop() 计数的流。
const getUserMediaCalls = [];
const trackStops = [];
function makeStream(trackCount = 1) {
  const tracks = [];
  for (let i = 0; i < trackCount; i += 1) {
    tracks.push({ stop: () => { trackStops.push(i); } });
  }
  return { getTracks: () => tracks.slice() };
}

// Node >= 21 自带只读的 navigator getter，必须 defineProperty 覆盖。
Object.defineProperty(globalThis, "navigator", {
  configurable: true,
  value: {
    mediaDevices: {
      getUserMedia: (constraints) => {
        getUserMediaCalls.push(constraints);
        return Promise.resolve(makeStream());
      },
    },
  },
});

// 假 WebSocket：记录构造 URL、send 参数与 close 调用。
class FakeWebSocket {
  constructor(url) {
    this.url = String(url);
    this.readyState = FakeWebSocket.OPEN;
    this.sent = [];
    this.closeCalls = 0;
    this.onopen = null;
    this.onclose = null;
    this.onerror = null;
    websocketInstances.push(this);
  }
  send(data) { this.sent.push(data); }
  close() { this.closeCalls += 1; this.readyState = FakeWebSocket.CLOSED; }
}
FakeWebSocket.OPEN = 1;
FakeWebSocket.CLOSED = 3;
global.WebSocket = FakeWebSocket;

// 假 canvas 2D 上下文与 toBlob 产物：一张"JPEG 帧"（SOI 开头，无任何前缀）。
const fakeCtx = { drawImage: () => {} };
const jpegBytes = new Uint8Array([0xff, 0xd8, 0xff, 0xe0, 0x01, 0x02, 0x03, 0x04]);
const fakeBlob = { arrayBuffer: () => Promise.resolve(jpegBytes.buffer) };
const blobCalls = [];
let playCalls = 0;
let websocketInstances = [];

/* ------------------------------------------------------- location & window */

const fakeWindow = new FakeElement("window");
fakeWindow.location = {
  origin: "https://wechat.example",
  protocol: "https:",
  host: "wechat.example",
  href: "https://wechat.example/index.html",
  pathname: "/index.html",
  hash: "",
};
fakeWindow.WECHAT_WEBCAM_FPS = 10;   // 定时器 100ms，断言更好写
global.window = fakeWindow;
global.location = fakeWindow.location;

/* -------------------------------------------------------------- run script */

const scriptPath = process.argv[2] ||
  path.join(__dirname, "wechat-webcam-forward.js");
const source = fs.readFileSync(scriptPath, "utf8");
const run = () => vm.runInThisContext(source, { filename: scriptPath });

function resetWebcamState() {
  delete fakeWindow.wechatWebcamForwardInstalled;
  byId = new Map();
  body = new FakeElement("body");
  intervals.clear();
  timeouts.clear();
  getUserMediaCalls.length = 0;
  trackStops.length = 0;
  blobCalls.length = 0;
  playCalls = 0;
  websocketInstances = [];
  fakeWindow.location.hash = "";
}

const flush = async () => {
  for (let i = 0; i < 40; i += 1) await new Promise((r) => setImmediate(r));
};

function group() {
  return byId.get("wechat-webcam-forward");
}

function button() {
  const g = group();
  if (!g) return null;
  return g.children.filter((child) => child.tagName === "BUTTON")[0] || null;
}

async function main() {

/* 1. 只读页面不创建任何 DOM ------------------------------------------------ */

fakeWindow.location.hash = "#shared";
run();
assert.equal(group(), undefined, "#shared creates no group");
assert.equal(byId.get("wechat-topbar"), undefined, "#shared creates no top bar");
assert.equal(fakeWindow.wechatWebcamForwardInstalled, undefined);

fakeWindow.location.hash = "#player2";
run();
assert.equal(group(), undefined, "#player creates no group");
assert.equal(fakeWindow.wechatWebcamForwardInstalled, undefined);

/* 2. 正常路径：按钮 → getUserMedia → WebSocket → 定时器发出裸 JPEG ---------- */

resetWebcamState();
run();
fireInterval(500);                       // boot 轮询建按钮

assert.ok(group(), "button group was created");
assert.ok(byId.get("wechat-topbar"), "shared top bar host was created");
assert.equal(group().parentNode, byId.get("wechat-topbar"));
assert.equal(button().textContent, "摄像头");
assert.equal(button().attributes["aria-pressed"], "false");

button().emit("click");
await flush();

assert.equal(getUserMediaCalls.length, 1, "getUserMedia called exactly once");
assert.deepEqual(getUserMediaCalls[0], {
  video: {
    width: { ideal: 640 },
    height: { ideal: 480 },
    frameRate: { ideal: 15, max: 15 },
  },
}, "fixed 640x480@15fps constraints, regardless of the fps hook");
assert.equal(websocketInstances.length, 1);
assert.equal(
  websocketInstances[0].url,
  "wss://wechat.example/wechat-webcam/",
  "https page opens a wss:// socket at the SUBFOLDER-safe path"
);
assert.equal(button().attributes["aria-pressed"], "true");
assert.equal(trackStops.length, 0, "tracks keep running while forwarding");

fireInterval(100);                       // 帧定时器（WECHAT_WEBCAM_FPS=10）
await flush();

assert.equal(websocketInstances[0].sent.length, 1, "one frame was sent");
assert.ok(
  websocketInstances[0].sent[0] instanceof ArrayBuffer,
  "frame is sent as a raw ArrayBuffer"
);
assert.deepEqual(
  [...new Uint8Array(websocketInstances[0].sent[0])],
  [...jpegBytes],
  "sent bytes are exactly the JPEG frame, no type prefix"
);
assert.deepEqual(blobCalls[blobCalls.length - 1], {
  type: "image/jpeg",
  quality: 0.6,
}, "canvas.toBlob uses image/jpeg at the default quality");
assert.ok(playCalls >= 1, "the offscreen <video> was asked to play");

/* 3. 再点一次：停 track、关 WebSocket、aria-pressed 复位 -------------------- */

button().emit("click");
await flush();

assert.equal(trackStops.length, 1, "every track was stopped");
assert.equal(websocketInstances[0].closeCalls, 1, "WebSocket was closed");
assert.equal(button().attributes["aria-pressed"], "false");
assert.ok(
  ![...intervals.values()].some((timer) => timer.ms === 100),
  "frame timer was cleared"
);
// 关闭后定时器不再产生任何帧。
fireInterval(100);
await flush();
assert.equal(
  websocketInstances[0].sent.length, 1,
  "no frame is sent after teardown"
);

/* 4. SUBFOLDER 场景：路径前缀必须保留在 WebSocket URL 里 -------------------- */

resetWebcamState();
fakeWindow.location.pathname = "/sub/dir/index.html";
fakeWindow.location.href = "https://wechat.example/sub/dir/index.html";
run();
fireInterval(500);
button().emit("click");
await flush();

assert.equal(websocketInstances.length, 1);
assert.equal(
  websocketInstances[0].url,
  "wss://wechat.example/sub/dir/wechat-webcam/",
  "deployment prefix is preserved in the socket URL"
);

/* 5. 重复注入同一页面是 no-op ---------------------------------------------- */

resetWebcamState();
fakeWindow.location.pathname = "/index.html";
fakeWindow.location.href = "https://wechat.example/index.html";
run();
fireInterval(500);
run();                                   // 第二次注入被 window 级守卫拦住
fireInterval(500);
assert.equal(group().children.length, 1, "no duplicate button");
assert.equal(websocketInstances.length, 0);

}

main().then(() => {
  console.log("wechat-webcam-forward DOM tests passed");
}).catch((error) => {
  console.error(error);
  process.exit(1);
});
