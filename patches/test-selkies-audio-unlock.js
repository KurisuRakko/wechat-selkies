#!/usr/bin/env node
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

class FakeTarget {
  constructor() {
    this.listeners = new Map();
  }

  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(listener);
  }

  emit(type, init = {}) {
    const event = Object.assign({ type }, init);
    for (const listener of this.listeners.get(type) || []) listener(event);
    return event;
  }
}

class FakeAudioContext {
  constructor(state = "suspended") {
    this.state = state;
    this.resumeCalls = 0;
    this.resumePromise = null;
  }

  resume() {
    this.resumeCalls += 1;
    if (this.resumePromise) return this.resumePromise;
    this.state = "running";
    return Promise.resolve();
  }
}

function loadGate({ hasBeenActive = false, visibilityState = "visible" } = {}) {
  const windowTarget = new FakeTarget();
  const documentTarget = new FakeTarget();
  documentTarget.visibilityState = visibilityState;
  windowTarget.window = windowTarget;
  windowTarget.navigator = { userActivation: { hasBeenActive } };
  windowTarget.document = documentTarget;
  const context = vm.createContext({
    window: windowTarget,
    document: documentTarget,
    navigator: windowTarget.navigator,
    Promise,
    Set,
    WeakMap,
    Object,
    TypeError,
  });
  const source = fs.readFileSync(__dirname + "/selkies-audio-unlock.js", "utf8");
  vm.runInContext(source, context);
  return { windowTarget, documentTarget, gate: windowTarget.wechatAudioGate, source, context };
}

async function tick() {
  await Promise.resolve();
  await Promise.resolve();
}

(async () => {
  {
    const { windowTarget, gate } = loadGate();
    let factories = 0;
    const context = new FakeAudioContext();
    const created = gate.create(() => {
      factories += 1;
      return context;
    });

    assert.equal(factories, 0, "context must not be constructed before activation");
    windowTarget.emit("pointerdown", { isTrusted: false });
    assert.equal(factories, 0, "synthetic events must not unlock audio");

    const event = windowTarget.emit("pointerdown", { isTrusted: true });
    assert.equal(event.defaultPrevented, undefined, "unlock must not consume the remote pointer event");
    assert.equal(await created, context);
    await tick();
    assert.equal(factories, 1);
    assert.equal(context.resumeCalls, 1, "new context should resume inside the gesture");

    windowTarget.emit("keydown", { isTrusted: true });
    assert.equal(factories, 1, "later gestures must not duplicate the context");
  }

  {
    const { windowTarget, gate } = loadGate();
    const context = new FakeAudioContext();
    let resolveResume;
    context.resumePromise = new Promise((resolve) => { resolveResume = resolve; });
    await gate.resume(context);
    await gate.resume(context);
    assert.equal(context.resumeCalls, 0, "resume must wait for activation");
    windowTarget.emit("touchend", { isTrusted: true });
    await tick();
    assert.equal(context.resumeCalls, 1, "queued resume calls must be deduplicated");
    resolveResume();
    await tick();
  }

  {
    const { gate } = loadGate({ hasBeenActive: true });
    let factories = 0;
    const context = await gate.create(() => {
      factories += 1;
      return new FakeAudioContext("running");
    });
    assert.ok(context);
    assert.equal(factories, 1, "sticky browser activation should allow immediate reconnect init");
  }

  {
    const { windowTarget, documentTarget, gate } = loadGate({ visibilityState: "hidden" });
    let factories = 0;
    const created = gate.create(() => {
      factories += 1;
      return new FakeAudioContext();
    });
    windowTarget.emit("pointerdown", { isTrusted: true });
    assert.equal(factories, 0, "a hidden page should not start audio");
    documentTarget.visibilityState = "visible";
    documentTarget.emit("visibilitychange");
    await created;
    assert.equal(factories, 1, "visible page should flush the activated factory");
  }

  {
    const loaded = loadGate();
    const first = loaded.gate;
    vm.runInContext(loaded.source, loaded.context);
    assert.equal(loaded.windowTarget.wechatAudioGate, first, "helper injection must be idempotent");
    assert.equal(loaded.windowTarget.listeners.get("pointerdown").length, 1);
  }

  console.log("selkies-audio-unlock tests passed");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
