/* Root-scoped Service Worker for WeChat Web Push notifications. */
"use strict";

var API_SUBSCRIPTION = "/wechat-notifications/api/subscription";

function normalizePayload(event) {
  var payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch (_) {
    payload = {};
  }
  return {
    title: String(payload.title || "微信新消息"),
    body: String(payload.body || "收到一条新私聊消息"),
    tag: String(payload.tag || "wechat-new-message"),
    timestamp: Number(payload.timestamp || Date.now()),
    url: new URL(String(payload.url || "/"), self.registration.scope).href,
    force: payload.force === true
  };
}

function isControllerUrl(value) {
  var hash = new URL(value).hash;
  return hash.indexOf("shared") === -1 && hash.indexOf("player") === -1;
}

async function hasFocusedClient() {
  var windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
  return windows.some(function (client) {
    return isControllerUrl(client.url) &&
      client.focused === true && client.visibilityState === "visible";
  });
}

self.addEventListener("push", function (event) {
  var payload = normalizePayload(event);
  event.waitUntil((async function () {
    if (!payload.force && await hasFocusedClient()) return;
    await self.registration.showNotification(payload.title, {
      body: payload.body,
      icon: new URL("icon.png", self.registration.scope).href,
      badge: new URL("icon.png", self.registration.scope).href,
      tag: payload.tag,
      renotify: true,
      timestamp: payload.timestamp,
      data: { url: payload.url }
    });
  }()));
});

self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  var target = event.notification.data && event.notification.data.url ?
    event.notification.data.url : self.registration.scope;
  event.waitUntil((async function () {
    var windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (var i = 0; i < windows.length; i += 1) {
      if (isControllerUrl(windows[i].url) &&
          new URL(windows[i].url).origin === new URL(target).origin) {
        if ("navigate" in windows[i] && windows[i].url !== target) {
          await windows[i].navigate(target);
        }
        return windows[i].focus();
      }
    }
    return self.clients.openWindow(target);
  }()));
});

self.addEventListener("pushsubscriptionchange", function (event) {
  event.waitUntil((async function () {
    var key = event.oldSubscription && event.oldSubscription.options ?
      event.oldSubscription.options.applicationServerKey : null;
    if (!key) return;
    var subscription = await self.registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: key
    });
    await fetch(API_SUBSCRIPTION, {
      method: "PUT",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(subscription.toJSON())
    });
  }()));
});
