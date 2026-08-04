#!/usr/bin/env node
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

class FakeStyle {
  removeProperty(name) {
    const camel = name.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
    delete this[camel];
  }
}

class FakeTarget {
  constructor(tagName) {
    this.tagName = tagName || "";
    this.listeners = new Map();
    this.style = new FakeStyle();
    this.value = "";
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

  appendChild(child) {
    child.parentElement = this;
    this.children.push(child);
  }

  focus() {
    document.activeElement = this;
  }

  setAttribute(name, value) {
    this[name] = value;
  }

  setSelectionRange(start, end) {
    this.selectionStart = start;
    this.selectionEnd = end;
  }
}

const parent = new FakeTarget("DIV");
let parentRect = { left: 0, top: 0, right: 1280, bottom: 720, width: 1280, height: 720 };
parent.getBoundingClientRect = () => parentRect;

const overlay = new FakeTarget("INPUT");
let overlayRect = { left: 0, top: 0, right: 1280, bottom: 720, width: 1280, height: 720 };
overlay.getBoundingClientRect = () => overlayRect;
parent.appendChild(overlay);

const elements = new Map([["overlayInput", overlay]]);
global.document = {
  readyState: "complete",
  activeElement: null,
  getElementById: (id) => elements.get(id) || null,
  createElement: (tagName) => {
    const element = new FakeTarget(tagName.toUpperCase());
    Object.defineProperty(element, "id", {
      get() { return this._id; },
      set(value) { this._id = value; elements.set(value, this); },
    });
    return element;
  },
  addEventListener: () => {},
};

const calls = [];
const inputHandler = {
  _compositionStart(event) { calls.push(["start", event.data]); },
  _compositionUpdate(event) { calls.push(["update", event.data]); },
  _compositionEnd(event) { calls.push(["end", event.data]); },
  _typeString() { throw new Error("IME proxy must never call _typeString"); },
};

const fakeWindow = new FakeTarget("WINDOW");
fakeWindow.webrtcInput = inputHandler;
global.window = fakeWindow;
global.location = { hash: "" };
global.setTimeout = (callback) => { callback(); return 1; };
global.setInterval = (callback) => { callback(); return 1; };
global.clearInterval = () => {};

const scriptPath = process.argv[2] || require("node:path").join(__dirname, "wechat-ime-anchor.js");
const script = fs.readFileSync(scriptPath, "utf8");
vm.runInThisContext(script, { filename: scriptPath });

const proxy = elements.get("wechatImeProxy");
assert.ok(proxy, "proxy textarea was created");
assert.equal(proxy.tagName, "TEXTAREA");
assert.equal(parent.children.filter((child) => child.id === "wechatImeProxy").length, 1);
assert.equal(proxy.style.width, "1px");
assert.equal(proxy.style.height, "20px");
assert.equal(proxy.style.pointerEvents, "none");
assert.equal(proxy.style.left, "512px");
assert.equal(proxy.style.top, "602px");

const mouseEvent = fakeWindow.emit("mousedown", {
  target: overlay,
  button: 0,
  clientX: 160,
  clientY: 180,
  preventDefault() { throw new Error("mousedown was prevented"); },
  stopPropagation() { throw new Error("mousedown propagation was stopped"); },
});
assert.equal(mouseEvent.target, overlay);
assert.equal(proxy.style.left, "160px");
assert.equal(proxy.style.top, "170px");
assert.equal(document.activeElement, proxy);

proxy.emit("compositionstart", { data: "" });
proxy.value = "中";
proxy.emit("input", { isComposing: true });
assert.equal(proxy.value, "中", "value was cleared during composition");
proxy.emit("compositionupdate", { data: "中" });
proxy.emit("compositionend", { data: "中" });
assert.deepEqual(calls, [["start", ""], ["update", "中"], ["end", "中"]]);
assert.equal(proxy.value, "", "committed value was not cleared");

overlayRect = { left: 20, top: 30, right: 660, bottom: 390, width: 640, height: 360 };
parentRect = { left: 0, top: 0, right: 680, bottom: 420, width: 680, height: 420 };
fakeWindow.emit("resize");
assert.equal(proxy.style.left, "100px");
assert.equal(proxy.style.top, "110px");

vm.runInThisContext(script, { filename: scriptPath });
assert.equal(parent.children.filter((child) => child.id === "wechatImeProxy").length, 1);
assert.equal((fakeWindow.listeners.get("mousedown") || []).length, 1);

console.log("wechat-ime-anchor DOM tests passed");
