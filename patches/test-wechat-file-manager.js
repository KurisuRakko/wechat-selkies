#!/usr/bin/env node
"use strict";

// DOM-level tests for patches/wechat-file-manager.js, in the same node:vm
// style as test-wechat-desktop-export.js: a hand-written FakeDOM, a stubbed
// fetch whose responses are controlled per test, and real script execution.
//
// The guarantees under test are the ones a screenshot would not catch:
// stale-response token invalidation, key handling staying off document, and
// the DownloadURL drag format surviving a rename.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

/* ------------------------------------------------------------ fake timers */

let sequence = 0;
const timeouts = new Map();
const intervals = new Map();

global.setTimeout = (callback, ms) => {
  const id = ++sequence;
  timeouts.set(id, { ms: Number(ms) || 0, callback });
  return id;
};
global.clearTimeout = (id) => { timeouts.delete(id); };
global.setInterval = (callback, ms) => {
  const id = ++sequence;
  intervals.set(id, { ms: Number(ms) || 0, callback });
  return id;
};
global.clearInterval = (id) => { intervals.delete(id); };

function fireInterval(ms) {
  for (const timer of intervals.values()) {
    if (timer.ms === ms) timer.callback();
  }
}

const flush = async () => {
  for (let i = 0; i < 40; i += 1) await new Promise((r) => setImmediate(r));
};

/* --------------------------------------------------------------- fake DOM */

class FakeElement {
  constructor(tagName) {
    this.tagName = String(tagName || "").toUpperCase();
    this.listeners = new Map();
    this.dataset = {};
    this.style = {};
    this.className = "";
    this.classList = {
      _set: new Set(),
      add: (...names) => names.forEach((n) => this.classList._set.add(n)),
      remove: (...names) => names.forEach((n) => this.classList._set.delete(n)),
      contains: (n) => this.classList._set.has(n),
    };
    this.children = [];
    this._textContent = "";
    this.attributes = {};
    this.parentNode = null;
    this.disabled = false;
    this.value = "";
    this.href = "";
    this.download = "";
  }

  // 与真实 DOM 语义一致：设置 textContent 会移除全部子节点（脚本用它清空
  // 列表与面包屑容器，旧的子节点必须被摘掉）。
  get textContent() { return this._textContent; }
  set textContent(value) {
    this._textContent = String(value);
    this.children = [];
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
  getAttribute(name) { return this.attributes[name] === undefined ? null : this.attributes[name]; }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  click() { downloads.push({ href: this.href, name: this.download }); }
  // 只支持本脚本实际用到的选择器：tagName 选择器（iframe）与 class 选择器。
  querySelector(selector) {
    if (/^[a-z][a-z0-9-]*$/.test(selector)) {
      return this.children.find((c) => c.tagName === selector.toUpperCase()) || null;
    }
    const cls = selector.replace(/^\./, "");
    return findByClass(this, cls) || null;
  }
}

const documentElement = new FakeElement("html");
let head = new FakeElement("head");
let body = new FakeElement("body");
let modals = [];
let downloads = [];

global.document = {
  readyState: "complete",
  head,
  body,
  documentElement,
  getElementById: (id) => {
    for (const el of [head, body]) {
      const found = findById(el, id);
      if (found) return found;
    }
    return null;
  },
  createElement: (tagName) => new FakeElement(tagName),
  querySelectorAll: (selector) => (selector === ".files-modal" ? modals : []),
  addEventListener: () => {},
};

function findById(el, id) {
  if (el.id === id) return el;
  for (const child of el.children) {
    const found = findById(child, id);
    if (found) return found;
  }
  return null;
}

global.MutationObserver = class {
  constructor(callback) { this.callback = callback; }
  observe() {}
};

const fakeWindow = new FakeElement("window");
fakeWindow.location = {
  origin: "https://wechat.example",
  href: "https://wechat.example/index.html",
};
global.window = fakeWindow;
global.location = fakeWindow.location;

/* ------------------------------------------------------------ fake network */

// 每个请求都 push 进 fetches；默认按 URL 从 listing 表取 JSON。
const fetches = [];
const listing = new Map();
let fetchMode = "ok";   // "ok" | "http-error" | "reject" | "not-json"
let httpStatus = 403;

global.fetch = (url, options) => {
  fetches.push({ url: String(url), options });
  if (fetchMode === "reject") return Promise.reject(new Error("network down"));
  if (fetchMode === "http-error") {
    return Promise.resolve({ ok: false, status: httpStatus, json: () => Promise.reject(new Error("no body")) });
  }
  if (fetchMode === "not-json") {
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.reject(new Error("bad json")) });
  }
  if (fetchMode === "manual") {
    return new Promise((resolve) => manualResolvers.push({ url: String(url), resolve }));
  }
  const list = listing.get(String(url));
  return Promise.resolve({
    ok: true,
    status: 200,
    json: () => Promise.resolve(list || []),
  });
};

const manualResolvers = [];

/* -------------------------------------------------------------- run script */

const scriptPath = process.argv[2] ||
  path.join(__dirname, "wechat-file-manager.js");
const source = fs.readFileSync(scriptPath, "utf8");
const run = () => vm.runInThisContext(source, { filename: scriptPath });

const docListeners = {};
const originalWarn = console.warn;
console.warn = (...args) => { /* 测试期静默 */ };

function reset() {
  fetches.length = 0;
  downloads.length = 0;
  manualResolvers.length = 0;
  listing.clear();
  modals = [];
  head = new FakeElement("head");
  body = new FakeElement("body");
  global.document.head = head;
  global.document.body = body;
  fetchMode = "ok";
  httpStatus = 403;
  for (const type of Object.keys(docListeners)) delete docListeners[type];
}

function makeModal() {
  const m = new FakeElement("div");
  m.className = "files-modal";
  const iframe = new FakeElement("iframe");
  iframe.setAttribute("src", "./files/");
  m.appendChild(iframe);
  return m;
}

function okResponse(list) {
  return { ok: true, status: 200, json: () => Promise.resolve(list || []) };
}

function findByClass(el, cls) {
  if (typeof el.className === "string" &&
      el.className.split(/\s+/).indexOf(cls) !== -1) return el;
  for (const child of el.children) {
    const found = findByClass(child, cls);
    if (found) return found;
  }
  return null;
}

function countByTag(el, tag) {
  let n = 0;
  (function walk(node) {
    if (node.tagName === tag.toUpperCase()) n += 1;
    for (const child of node.children) walk(child);
  })(el);
  return n;
}

function findAllByClass(el, cls) {
  const out = [];
  (function walk(node) {
    if (typeof node.className === "string" &&
        node.className.split(/\s+/).indexOf(cls) !== -1) out.push(node);
    for (const child of node.children) walk(child);
  })(el);
  return out;
}

function rowNames(modal) {
  return findAllByClass(modal, "wfm-row")
    .map((row) => row.children[0].textContent);
}

function sizeTexts(modal) {
  return findAllByClass(modal, "wfm-row")
    .map((row) => row.children[3].textContent);
}

async function main() {
  let modal;
  let modal2;

  /* 1. 出现即接管：iframe 移除、div.wfm 挂上、style 只注入一次 ------------- */

  reset();
  modal = makeModal();
  modals.push(modal);
  run();
  fireInterval(2000);
  await flush();

  assert.equal(modal.querySelector("iframe"), null, "iframe was removed");
  assert.ok(findByClass(modal, "wfm"), "div.wfm was mounted");
  assert.equal(modal.dataset.wechatFileManager, "1", "modal is marked taken over");
  assert.equal(countByTag(document.head, "style"), 1, "style injected once");
  fireInterval(2000);
  fireInterval(2000);
  assert.equal(countByTag(document.head, "style"), 1, "style not re-injected");

  modal2 = makeModal();
  modals.push(modal2);
  fireInterval(2000);
  await flush();
  assert.equal(modal2.dataset.wechatFileManager, "1", "a second modal is also taken over");

  /* 2. 渲染：隐藏项不出现、目录在前、大小格式化 ----------------------------- */

  reset();
  listing.set("./files/", [
    { name: ".hidden", type: "file", size: 1, mtime: "Mon, 10 Aug 2026 08:18:51 GMT" },
    { name: "folder", type: "directory", mtime: "Mon, 10 Aug 2026 08:18:51 GMT" },
    { name: "a.png", type: "file", mtime: "Mon, 10 Aug 2026 08:18:51 GMT", size: 1536 },
    { name: "中文 报告.txt", type: "file", mtime: "Mon, 10 Aug 2026 08:18:51 GMT", size: 5 },
    { name: "10.png", type: "file", mtime: "Mon, 10 Aug 2026 08:18:51 GMT", size: 10 },
    { name: "2.png", type: "file", mtime: "Mon, 10 Aug 2026 08:18:51 GMT", size: 20 },
  ]);
  modal = makeModal();
  modals.push(modal);
  run();
  fireInterval(2000);
  await flush();

  const names = rowNames(modal);
  assert.equal(names.length, 5, ".hidden is filtered out");
  assert.ok(names.every((n) => n.indexOf(".hidden") === -1), "no hidden entry");
  assert.ok(names[0].indexOf("folder") !== -1, "directory sorts before files");
  const idx2 = names.findIndex((n) => n.indexOf("2.png") !== -1);
  const idx10 = names.findIndex((n) => n.indexOf("10.png") !== -1);
  assert.ok(idx2 >= 0 && idx2 < idx10, "natural order: 2.png before 10.png");
  assert.ok(sizeTexts(modal).indexOf("1.5 KB") !== -1, "1536 bytes formats as 1.5 KB");
  assert.ok(sizeTexts(modal).indexOf("—") !== -1, "directories show — for size");

  /* 3. 双击目录进入、面包屑两段、上级按钮、回根目录 -------------------------- */

  reset();
  listing.set("./files/", [
    { name: "dirA", type: "directory", mtime: "Mon, 10 Aug 2026 08:18:51 GMT" },
  ]);
  listing.set("./files/dirA/", []);
  modal = makeModal();
  modals.push(modal);
  run();
  fireInterval(2000);
  await flush();

  const dirRow = findAllByClass(modal, "wfm-row")[0];
  dirRow.emit("dblclick");
  await flush();
  assert.equal(fetches[1].url, "./files/" + encodeURIComponent("dirA") + "/",
    "entering a directory fetches its own URL");
  const crumbs = findAllByClass(modal, "wfm-crumb").map((c) => c.textContent);
  assert.deepEqual(crumbs, ["桌面", "dirA"], "breadcrumb has two segments");
  const upBtn = findAllByClass(modal, "wfm-btn")[0];
  assert.equal(upBtn.disabled, false, "up button became enabled");
  upBtn.emit("click");
  await flush();
  assert.equal(fetches[2].url, "./files/", "up returns to the root listing");
  assert.equal(findAllByClass(modal, "wfm-btn")[0].disabled, true, "up disabled at root");

  /* 4. 双击文件下载：href 正确 percent-encoding、download 等于原名 ------------ */

  for (const file of ["a b.png", "报#告.md", "报告.md"]) {
    reset();
    listing.set("./files/", [
      { name: file, type: "file", mtime: "Mon, 10 Aug 2026 08:18:51 GMT", size: 100 },
    ]);
    modal = makeModal();
  modals.push(modal);
    run();
    fireInterval(2000);
    await flush();
    findAllByClass(modal, "wfm-row")[0].emit("dblclick");
    await flush();
    assert.equal(downloads.length, 1, "one download click for " + file);
    assert.equal(downloads[0].href, "./files/" + encodeURIComponent(file),
      "href is percent-encoded for " + file);
    assert.equal(downloads[0].name, file, "download attribute keeps the original name");
  }

  /* 5. dragstart：DownloadURL 格式、目录行不可拖 ----------------------------- */

  reset();
  listing.set("./files/", [
    { name: "a b.png", type: "file", mtime: "Mon, 10 Aug 2026 08:18:51 GMT", size: 100 },
    { name: "folder", type: "directory", mtime: "Mon, 10 Aug 2026 08:18:51 GMT" },
  ]);
  modal = makeModal();
  modals.push(modal);
  run();
  fireInterval(2000);
  await flush();

  const rows5 = findAllByClass(modal, "wfm-row");
  const fileRow = rows5.find((r) => r.children[0].textContent.indexOf("a b.png") !== -1);
  const dt = { sets: {}, effectAllowed: "" };
  dt.setData = (key, value) => { dt.sets[key] = value; };
  fileRow.emit("dragstart", { dataTransfer: dt });
  assert.equal(dt.sets["DownloadURL"],
    "image/png:a b.png:https://wechat.example/files/a%20b.png",
    "DownloadURL is mime:name:absolute-url");
  assert.equal(dt.sets["text/uri-list"], "https://wechat.example/files/a%20b.png");
  assert.equal(dt.sets["text/plain"], "https://wechat.example/files/a%20b.png");
  assert.equal(dt.effectAllowed, "copy");
  const dirRow5 = rows5.find((r) => r.children[0].textContent.indexOf("folder") !== -1);
  assert.equal(dirRow5.getAttribute("draggable"), null, "directory rows are not draggable");

  /* 6. 表头排序：点大小升序再点降序，目录始终在前 ---------------------------- */

  reset();
  listing.set("./files/", [
    { name: "dirA", type: "directory", mtime: "Mon, 10 Aug 2026 08:18:51 GMT" },
    { name: "b.txt", type: "file", mtime: "Mon, 10 Aug 2026 08:18:51 GMT", size: 2048 },
    { name: "a.png", type: "file", mtime: "Mon, 10 Aug 2026 08:18:51 GMT", size: 10 },
    { name: "c.bin", type: "file", mtime: "Mon, 10 Aug 2026 08:18:51 GMT", size: 5 },
  ]);
  modal = makeModal();
  modals.push(modal);
  run();
  fireInterval(2000);
  await flush();

  const sizeTh = findAllByClass(modal, "wfm-th")[3];
  sizeTh.emit("click");
  assert.deepEqual(sizeTexts(modal), ["—", "5 B", "10 B", "2.0 KB"], "size ascending");
  assert.ok(rowNames(modal)[0].indexOf("dirA") !== -1, "directory stays first ascending");
  sizeTh.emit("click");
  assert.deepEqual(sizeTexts(modal), ["—", "2.0 KB", "10 B", "5 B"], "size descending");
  assert.ok(rowNames(modal)[0].indexOf("dirA") !== -1, "directory stays first descending");

  /* 7. 筛选只保留匹配项，状态栏随之变化 -------------------------------------- */

  reset();
  listing.set("./files/", [
    { name: "a.png", type: "file", mtime: "Mon, 10 Aug 2026 08:18:51 GMT", size: 10 },
    { name: "b.txt", type: "file", mtime: "Mon, 10 Aug 2026 08:18:51 GMT", size: 10 },
  ]);
  modal = makeModal();
  modals.push(modal);
  run();
  fireInterval(2000);
  await flush();
  const filterInput = findByClass(modal, "wfm-filter");
  filterInput.value = "png";
  filterInput.emit("input");
  assert.deepEqual(rowNames(modal), ["🖼 a.png"], "filter keeps only matches");
  assert.equal(findByClass(modal, "wfm-status").textContent, "1 个项目",
    "status bar counts filtered items");

  /* 8. HTTP 403 → 错误态含状态码，重试会再发请求 ----------------------------- */

  reset();
  fetchMode = "http-error";
  httpStatus = 403;
  modal = makeModal();
  modals.push(modal);
  run();
  fireInterval(2000);
  await flush();
  const errLine = findByClass(modal, "wfm-error");
  assert.ok(errLine && errLine.textContent.indexOf("403") !== -1, "error shows HTTP 403");
  fetchMode = "ok";
  listing.set("./files/", [
    { name: "a.png", type: "file", mtime: "Mon, 10 Aug 2026 08:18:51 GMT", size: 10 },
  ]);
  const retry = findAllByClass(modal, "wfm-btn").find((b) => b.textContent === "重试");
  assert.ok(retry, "retry button exists");
  retry.emit("click");
  await flush();
  assert.equal(fetches.length, 2, "retry issues a fresh request");
  assert.equal(findAllByClass(modal, "wfm-row").length, 1, "listing renders after retry");

  /* 9. 过期响应不覆盖后返回的结果（token 作废） ------------------------------ */

  reset();
  fetchMode = "manual";
  listing.set("./files/", [
    { name: "dirA", type: "directory", mtime: "Mon, 10 Aug 2026 08:18:51 GMT" },
    { name: "dirB", type: "directory", mtime: "Mon, 10 Aug 2026 08:18:51 GMT" },
  ]);
  modal = makeModal();
  modals.push(modal);
  run();
  fireInterval(2000);
  await flush();
  manualResolvers[0].resolve(okResponse(listing.get("./files/")));
  await flush();
  const rows9 = findAllByClass(modal, "wfm-row");
  rows9[0].emit("dblclick");   // 进入 dirA
  rows9[1].emit("dblclick");   // 立刻再进 dirB
  await flush();
  assert.equal(fetches.length, 3, "root + two directory requests");
  // 后发的请求先返回。
  manualResolvers[2].resolve(okResponse([{ name: "inB.txt", type: "file", mtime: "x", size: 1 }]));
  await flush();
  assert.deepEqual(rowNames(modal), ["📄 inB.txt"], "newer response renders");
  // 先发的请求后返回，必须被作废。
  manualResolvers[1].resolve(okResponse([{ name: "inA.txt", type: "file", mtime: "x", size: 1 }]));
  await flush();
  assert.deepEqual(rowNames(modal), ["📄 inB.txt"], "stale response does not overwrite");

  /* 10. 键盘：列表聚焦时生效，handler 不在 document 上 ----------------------- */

  reset();
  listing.set("./files/", [
    { name: "dirA", type: "directory", mtime: "Mon, 10 Aug 2026 08:18:51 GMT" },
    { name: "a.png", type: "file", mtime: "Mon, 10 Aug 2026 08:18:51 GMT", size: 10 },
  ]);
  modal = makeModal();
  modals.push(modal);
  run();
  fireInterval(2000);
  await flush();

  const list = findByClass(modal, "wfm-list");
  assert.ok(list, "list container exists");
  list.emit("keydown", { key: "ArrowDown", preventDefault: () => {} });
  assert.ok(findAllByClass(modal, "wfm-row")[0].classList.contains("wfm-selected"),
    "ArrowDown selects the first row");
  list.emit("keydown", { key: "ArrowDown", preventDefault: () => {} });
  assert.ok(findAllByClass(modal, "wfm-row")[1].classList.contains("wfm-selected"),
    "ArrowDown moves selection down");
  list.emit("keydown", { key: "Enter", preventDefault: () => {} });
  await flush();
  assert.equal(downloads.length, 1, "Enter downloads the selected file");
  assert.equal(downloads[0].href, "./files/a.png", "Enter downloads the right file");
  // 进入目录后 Backspace 返回上级。
  findAllByClass(modal, "wfm-row")[0].emit("dblclick");
  await flush();
  assert.equal(fetches[fetches.length - 1].url, "./files/dirA/", "entered dirA");
  list.emit("keydown", { key: "Backspace", preventDefault: () => {} });
  await flush();
  assert.equal(fetches[fetches.length - 1].url, "./files/", "Backspace goes up");
  assert.equal(docListeners.keydown, undefined, "no keydown handler on document");
}

main().then(() => {
  process.stdout.write("wechat-file-manager DOM tests: OK (10 cases)\n");
}).catch((error) => {
  originalWarn(error);
  process.exit(1);
});
