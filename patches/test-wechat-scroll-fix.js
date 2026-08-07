#!/usr/bin/env node
"use strict";

/*
 * 对 patches/wechat-scroll-fix.js 的文本级测试，node 可直接运行。
 *
 * 覆盖的回归点：
 *   - 像素累积跨过阈值发 tick，并且保留余数；
 *   - deltaMode 1/2 正确换算成像素；
 *   - 一次事件多个 tick 合并成单次 magnitude；
 *   - 方向反转时累加器清零；
 *   - 上游 10ms release 窗口内不重复触发，退回像素随下一事件冲刷；
 *   - 超过 MAX_MAGNITUDE 的整 tick 被丢弃而不是延后；
 *   - webrtcInput 或私有 wheel API 缺失时放行原生事件。
 */

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

/* ------------------------------------------------------------ fake DOM */

class FakeElement {
  constructor(name) {
    this.name = name;
    this.children = [];
    this.clientHeight = 0;
  }

  contains(node) {
    if (node === this) return true;
    return this.children.some((child) => child.contains(node));
  }
}

const overlay = new FakeElement("overlay");
overlay.clientHeight = 800;
const outside = new FakeElement("outside");
const child = new FakeElement("child");
overlay.children.push(child);

const wheelListeners = [];
const sent = [];

function makeHandler() {
  return {
    element: overlay,
    buttonMask: 0,
    _triggerMouseWheel(direction, magnitude) {
      sent.push(["v", direction, magnitude]);
    },
    _triggerHorizontalMouseWheel(direction, magnitude) {
      sent.push(["h", direction, magnitude]);
    }
  };
}

const fakeWindow = {
  innerHeight: 900,
  WECHAT_SCROLL_PIXELS_PER_TICK: 40,
  WECHAT_SCROLL_MAX_MAGNITUDE: 10,
  webrtcInput: makeHandler(),
  addEventListener(type, listener, options) {
    if (type === "wheel") wheelListeners.push({ listener, options });
  }
};

global.window = fakeWindow;
global.location = fakeWindow.location = { hash: "" };

function emitWheel(init) {
  const event = Object.assign({
    type: "wheel",
    target: overlay,
    deltaY: 0,
    deltaX: 0,
    deltaMode: 0,
    cancelable: true,
    defaultPrevented: false,
    propagationStopped: false,
    preventDefault() { this.defaultPrevented = true; },
    stopPropagation() { this.propagationStopped = true; }
  }, init || {});
  for (const registration of wheelListeners) registration.listener(event);
  return event;
}

/* -------------------------------------------------------------- run script */

const scriptPath = process.argv[2] || path.join(__dirname, "wechat-scroll-fix.js");
vm.runInThisContext(fs.readFileSync(scriptPath, "utf8"), { filename: scriptPath });

assert.equal(wheelListeners.length, 1, "wheel capture listener registered");
assert.equal(wheelListeners[0].options.capture, true, "listener uses capture");
assert.equal(wheelListeners[0].options.passive, false, "listener is not passive");

/* ------------------------------------------------------------------- tests */

function main() {
  /* 1. 像素累积跨阈值发 tick，余数保留到下一次 -------------------------- */

  let ev = emitWheel({ deltaY: 100 });
  assert.equal(ev.defaultPrevented, true, "intercepted event is prevented");
  assert.equal(ev.propagationStopped, true, "intercepted event stops propagation");
  assert.deepEqual(sent, [["v", "down", 2]], "100px becomes two ticks, one send");

  emitWheel({ deltaY: 25 });
  assert.deepEqual(
    sent,
    [["v", "down", 2], ["v", "down", 1]],
    "remaining 20px plus 25px crosses the 40px threshold once"
  );

  emitWheel({ deltaY: 5 });
  assert.equal(sent.length, 2, "sub-threshold delta sends nothing");

  /* 2. 方向反转时累加器清零 ---------------------------------------------- */

  emitWheel({ deltaY: -15 });
  assert.equal(sent.length, 2, "reverse starts from zero and does not consume old remainder");

  emitWheel({ deltaY: -25 });
  assert.deepEqual(sent[2], ["v", "up", 1], "reverse accumulator still sends ticks");

  /* 3. deltaMode 1: 行换算成 40px ---------------------------------------- */

  emitWheel({ deltaY: 1, deltaMode: 1 });
  assert.deepEqual(sent[3], ["v", "down", 1], "one line becomes one 40px tick");

  emitWheel({ deltaY: -2, deltaMode: 1 });
  assert.deepEqual(sent[4], ["v", "up", 2], "two lines becomes two ticks");

  /* 4. deltaMode 2: 页换算成元素高度，并限制单次 magnitude ---------------- */

  emitWheel({ deltaY: 1, deltaMode: 2 });
  assert.deepEqual(sent[5], ["v", "down", 10], "one page is capped at max magnitude");

  emitWheel({ deltaY: 1 });
  assert.equal(sent.length, 6, "overflow ticks are discarded; 1px after a capped page does not burst");

  /* 5. 横向滚轮走独立的累加器 --------------------------------------------- */

  emitWheel({ deltaX: 80 });
  assert.deepEqual(sent[6], ["h", "right", 2], "horizontal delta sends right ticks");

  emitWheel({ deltaX: -120 });
  assert.deepEqual(sent[7], ["h", "left", 3], "horizontal reverse sends left ticks");

  /* 6. element 内的子节点也算在接管范围内 --------------------------------- */

  emitWheel({ target: child, deltaX: 40 });
  assert.deepEqual(sent[8], ["h", "right", 1], "child target inside element is handled");

  /* 7. element 外的 wheel 事件放行给原生页面 ------------------------------- */

  const beforeOutside = sent.length;
  const outsideEvent = emitWheel({ target: outside, deltaY: 120 });
  assert.equal(outsideEvent.defaultPrevented, false, "outside target is not prevented");
  assert.equal(outsideEvent.propagationStopped, false, "outside target still propagates");
  assert.equal(sent.length, beforeOutside, "outside target sends nothing");

  /* 8. 私有 wheel API 缺失时降级为原生处理 -------------------------------- */

  sent.length = 0;
  window.webrtcInput = { element: overlay };
  const missingApiEvent = emitWheel({ deltaY: 120 });
  assert.equal(missingApiEvent.defaultPrevented, false, "missing API does not preventDefault");
  assert.equal(missingApiEvent.propagationStopped, false, "missing API does not stop propagation");
  assert.deepEqual(sent, [], "missing API sends nothing");

  /* 9. webrtcInput 本身缺失时也放行 --------------------------------------- */

  window.webrtcInput = null;
  const noHandlerEvent = emitWheel({ deltaY: 120 });
  assert.equal(noHandlerEvent.defaultPrevented, false, "missing handler does not preventDefault");
  assert.equal(noHandlerEvent.propagationStopped, false, "missing handler still propagates");

  /* 10. 不可取消的事件不拦截，避免吞掉浏览器无法阻止的滚动 ----------------- */

  window.webrtcInput = makeHandler();
  const notCancelableEvent = emitWheel({ cancelable: false, deltaY: 40 });
  assert.equal(notCancelableEvent.defaultPrevented, false, "non-cancelable event is left alone");
  assert.equal(notCancelableEvent.propagationStopped, false, "non-cancelable event propagates");
  assert.equal(sent.length, 0, "non-cancelable event sends nothing");

  /* 11. 每次事件动态读取 webrtcInput，不缓存旧实例 ------------------------- */

  const firstCalls = [];
  const secondCalls = [];
  window.webrtcInput = {
    element: overlay,
    _triggerMouseWheel(direction, magnitude) { firstCalls.push([direction, magnitude]); },
    _triggerHorizontalMouseWheel(direction, magnitude) { firstCalls.push(["h", direction, magnitude]); }
  };
  emitWheel({ deltaX: 40 });

  window.webrtcInput = {
    element: overlay,
    _triggerMouseWheel(direction, magnitude) { secondCalls.push([direction, magnitude]); },
    _triggerHorizontalMouseWheel(direction, magnitude) { secondCalls.push(["h", direction, magnitude]); }
  };
  emitWheel({ deltaX: 40 });

  assert.deepEqual(firstCalls, [["h", "right", 1]], "old handler receives its own event");
  assert.deepEqual(secondCalls, [["h", "right", 1]], "replaced handler is read dynamically");

  /* 12. 上游 10ms release 窗口内不发送，退回像素随下一空闲事件冲刷 --------- */

  sent.length = 0;
  window.webrtcInput = {
    element: overlay,
    buttonMask: 1 << 3,
    _triggerMouseWheel(direction, magnitude) { sent.push(["v", direction, magnitude]); },
    _triggerHorizontalMouseWheel(direction, magnitude) { sent.push(["h", direction, magnitude]); }
  };
  emitWheel({ deltaY: 100 });
  assert.deepEqual(sent, [], "held down button suppresses the send");

  window.webrtcInput.buttonMask = 0;
  emitWheel({ deltaY: 1 });
  assert.deepEqual(sent, [["v", "down", 2]], "returned ticks flush on the next idle event");

  /* 13. 横向也检查 buttonMask 占用 ----------------------------------------- */

  sent.length = 0;
  window.webrtcInput.buttonMask = 1 << 6;
  emitWheel({ deltaX: -80 });
  assert.deepEqual(sent, [], "held left button suppresses the send");

  window.webrtcInput.buttonMask = 0;
  emitWheel({ deltaX: -1 });
  assert.deepEqual(sent, [["h", "left", 2]], "horizontal returned ticks flush on release");

  console.log("wechat-scroll-fix DOM tests passed");
}

main();
