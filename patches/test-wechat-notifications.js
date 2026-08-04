"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const pageSource = fs.readFileSync(path.join(__dirname, "wechat-notifications.js"), "utf8");
const workerSource = fs.readFileSync(path.join(__dirname, "wechat-notification-sw.js"), "utf8");

function flush() {
  return new Promise((resolve) => setImmediate(resolve));
}

function pageContext(hash = "") {
  const elements = new Map();
  const fetches = [];
  const storage = new Map();
  const subscription = {
    endpoint: "https://push.example/send/1",
    options: { applicationServerKey: null },
    toJSON() {
      return {
        endpoint: this.endpoint,
        expirationTime: null,
        keys: { p256dh: "key", auth: "auth" }
      };
    }
  };
  const registration = {
    pushManager: {
      async getSubscription() { return null; },
      async subscribe(options) {
        subscription.options.applicationServerKey = options.applicationServerKey;
        return subscription;
      }
    }
  };
  const body = {
    appendChild(node) {
      node.parentNode = body;
      elements.set(node.id, node);
    },
    removeChild(node) {
      node.parentNode = null;
      elements.delete(node.id);
    }
  };
  const document = {
    readyState: "complete",
    body,
    getElementById(id) { return elements.get(id) || null; },
    createElement(tag) {
      return {
        tagName: tag.toUpperCase(),
        style: {},
        children: [],
        appendChild(child) { this.children.push(child); },
        setAttribute() {}
      };
    },
    addEventListener() {}
  };
  const context = {
    console,
    document,
    location: { hash, origin: "https://wechat.example" },
    navigator: {
      serviceWorker: {
        async register() { return registration; },
        ready: Promise.resolve(registration)
      }
    },
    Notification: {
      permission: "default",
      async requestPermission() {
        context.Notification.permission = "granted";
        return "granted";
      }
    },
    PushManager: function PushManager() {},
    localStorage: {
      getItem(key) { return storage.get(key) || null; },
      setItem(key, value) { storage.set(key, String(value)); }
    },
    fetch: async (url, options = {}) => {
      fetches.push({ url, options });
      let payload = { ok: true };
      if (url.endsWith("/config")) {
        payload = {
          ready: true,
          vapidPublicKey: Buffer.from(Buffer.concat([Buffer.from([4]), Buffer.alloc(64, 2)]))
            .toString("base64url")
        };
      }
      return {
        ok: true,
        status: url.endsWith("/test") ? 202 : 200,
        async json() { return payload; },
        async text() { return ""; }
      };
    },
    atob(value) { return Buffer.from(value, "base64").toString("binary"); },
    btoa(value) { return Buffer.from(value, "binary").toString("base64"); },
    setTimeout(callback) { callback(); return 1; },
    clearTimeout() {},
    URL,
    Uint8Array,
    Object,
    Promise,
    Error
  };
  context.window = context;
  context.window.isSecureContext = true;
  return { context: vm.createContext(context), elements, fetches, storage };
}

async function testPageEnrollment() {
  const fixture = pageContext();
  vm.runInContext(pageSource, fixture.context, { filename: "wechat-notifications.js" });
  const banner = fixture.elements.get("wechatNotificationsBanner");
  assert(banner, "default permission should show the explicit enable banner");
  await banner._wechatButton.onclick();
  await flush();
  await flush();
  assert(fixture.fetches.some((entry) => entry.url.endsWith("/config")));
  const put = fixture.fetches.find((entry) => entry.options.method === "PUT");
  assert(put, "subscription must be registered with the container API");
  assert.strictEqual(JSON.parse(put.options.body).endpoint, "https://push.example/send/1");
  assert(fixture.fetches.some((entry) => entry.url.endsWith("/test")));
  assert.strictEqual(fixture.storage.get("wechatNotificationsEnabled"), "1");
}

function testViewerIsExcluded() {
  const fixture = pageContext("#shared");
  vm.runInContext(pageSource, fixture.context, { filename: "wechat-notifications.js" });
  assert.strictEqual(fixture.context.__wechatNotificationsInstalled, undefined);
  assert.strictEqual(fixture.elements.size, 0);
}

function workerContext() {
  const handlers = {};
  const shown = [];
  const fetches = [];
  const windows = [];
  const renewed = {
    endpoint: "https://push.example/send/new",
    toJSON() { return { endpoint: this.endpoint, keys: { p256dh: "p", auth: "a" } }; }
  };
  const self = {
    addEventListener(name, handler) { handlers[name] = handler; },
    clients: {
      async matchAll() { return windows; },
      async openWindow(url) { return { opened: url }; }
    },
    registration: {
      scope: "https://wechat.example/",
      async showNotification(title, options) { shown.push({ title, options }); },
      pushManager: { async subscribe() { return renewed; } }
    }
  };
  const context = vm.createContext({
    self,
    URL,
    Date,
    String,
    Number,
    JSON,
    fetch: async (url, options) => { fetches.push({ url, options }); return { ok: true }; }
  });
  vm.runInContext(workerSource, context, { filename: "wechat-notification-sw.js" });
  return { handlers, shown, windows, fetches };
}

async function dispatchPush(fixture, payload) {
  let promise;
  fixture.handlers.push({
    data: { json() { return payload; } },
    waitUntil(value) { promise = value; }
  });
  await promise;
}

async function testWorkerForegroundAndBackground() {
  const fixture = workerContext();
  fixture.windows.push({ focused: true, visibilityState: "visible", url: "https://wechat.example/" });
  await dispatchPush(fixture, { title: "Alice", body: "hello", tag: "one" });
  assert.strictEqual(fixture.shown.length, 0, "focused controller should suppress real pushes");
  fixture.windows[0].url = "https://wechat.example/#shared";
  await dispatchPush(fixture, { title: "Alice", body: "hello", tag: "shared" });
  assert.strictEqual(fixture.shown.length, 1, "focused shared viewer must not suppress owner notifications");
  fixture.windows[0].url = "https://wechat.example/";
  fixture.windows[0].focused = false;
  await dispatchPush(fixture, { title: "Alice", body: "hello", tag: "one" });
  assert.strictEqual(fixture.shown.length, 2);
  assert.strictEqual(fixture.shown[1].options.tag, "one");
  fixture.windows[0].focused = true;
  await dispatchPush(fixture, { title: "Enabled", force: true, tag: "test" });
  assert.strictEqual(fixture.shown.length, 3, "enrollment test must bypass foreground suppression");
}

async function testWorkerClickAndRenewal() {
  const fixture = workerContext();
  let focused = false;
  fixture.windows.push({
    focused: false,
    visibilityState: "hidden",
    url: "https://wechat.example/",
    async focus() { focused = true; },
    async navigate() {}
  });
  let clickPromise;
  fixture.handlers.notificationclick({
    notification: {
      data: { url: "https://wechat.example/" },
      close() {}
    },
    waitUntil(value) { clickPromise = value; }
  });
  await clickPromise;
  assert.strictEqual(focused, true);

  let renewalPromise;
  fixture.handlers.pushsubscriptionchange({
    oldSubscription: { options: { applicationServerKey: new Uint8Array([1, 2, 3]) } },
    waitUntil(value) { renewalPromise = value; }
  });
  await renewalPromise;
  assert.strictEqual(fixture.fetches.length, 1);
  assert.strictEqual(fixture.fetches[0].options.method, "PUT");
}

(async function main() {
  await testPageEnrollment();
  testViewerIsExcluded();
  await testWorkerForegroundAndBackground();
  await testWorkerClickAndRenewal();
  console.log("wechat notification browser tests passed");
}()).catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
