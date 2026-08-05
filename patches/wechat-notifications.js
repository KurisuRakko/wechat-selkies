/* Enable same-origin Web Push notifications for private WeChat messages. */
(function () {
  "use strict";

  var TAG = "[wechat-notifications]";
  var BANNER_ID = "wechatNotificationsBanner";
  var API_ROOT = "/wechat-notifications/api/";
  var WORKER_URL = "/wechat-notification-sw.js";
  var ENABLED_KEY = "wechatNotificationsEnabled";

  if (String(location.hash).indexOf("shared") !== -1 ||
      String(location.hash).indexOf("player") !== -1) {
    return;
  }
  if (window.__wechatNotificationsInstalled) return;
  window.__wechatNotificationsInstalled = true;

  function supported() {
    return window.isSecureContext &&
      "Notification" in window &&
      "serviceWorker" in navigator &&
      "PushManager" in window;
  }

  function base64UrlToBytes(value) {
    var padded = value + "=".repeat((4 - value.length % 4) % 4);
    var binary = atob(padded.replace(/-/g, "+").replace(/_/g, "/"));
    var bytes = new Uint8Array(binary.length);
    for (var i = 0; i < binary.length; i += 1) {
      bytes[i] = binary.charCodeAt(i);
    }
    return bytes;
  }

  function bytesToBase64Url(value) {
    if (!value) return "";
    var bytes = new Uint8Array(value);
    var binary = "";
    for (var i = 0; i < bytes.length; i += 1) {
      binary += String.fromCharCode(bytes[i]);
    }
    return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  async function api(path, options) {
    var response = await fetch(API_ROOT + path, Object.assign({
      credentials: "same-origin",
      cache: "no-store"
    }, options || {}));
    if (!response.ok) {
      var message = await response.text().catch(function () { return ""; });
      throw new Error("notification API " + response.status + (message ? ": " + message : ""));
    }
    return response.json();
  }

  async function getReadyConfig(waitForMonitor) {
    var attempts = waitForMonitor ? 10 : 1;
    var config = null;
    for (var i = 0; i < attempts; i += 1) {
      config = await api("config");
      if (config.ready && config.vapidPublicKey) return config;
      if (i + 1 < attempts) {
        await new Promise(function (resolve) { setTimeout(resolve, 1000); });
      }
    }
    var reason = config && config.error && config.error.message;
    throw new Error(reason ? "微信消息读取器尚未就绪：" + reason : "微信消息读取器尚未就绪");
  }

  async function ensureSubscription(config) {
    await navigator.serviceWorker.register(WORKER_URL, { scope: "/" });
    var registration = await navigator.serviceWorker.ready;
    var subscription = await registration.pushManager.getSubscription();
    if (subscription && subscription.options && subscription.options.applicationServerKey) {
      var currentKey = bytesToBase64Url(subscription.options.applicationServerKey);
      if (currentKey && currentKey !== config.vapidPublicKey) {
        await subscription.unsubscribe();
        subscription = null;
      }
    }
    if (!subscription) {
      subscription = await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: base64UrlToBytes(config.vapidPublicKey)
      });
    }
    await api("subscription", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(subscription.toJSON())
    });
    return subscription;
  }

  function removeBannerSoon(banner) {
    setTimeout(function () {
      if (banner && banner.parentNode) banner.parentNode.removeChild(banner);
    }, 2400);
  }

  function createBanner() {
    var existing = document.getElementById(BANNER_ID);
    if (existing) return existing;
    var banner = document.createElement("div");
    banner.id = BANNER_ID;
    banner.setAttribute("role", "status");
    banner.style.cssText = [
      "position:fixed",
      "left:50%",
      "bottom:18px",
      "transform:translateX(-50%)",
      "z-index:2147483647",
      "display:flex",
      "align-items:center",
      "gap:10px",
      "max-width:calc(100vw - 32px)",
      "padding:10px 12px",
      "border:1px solid rgba(255,255,255,.18)",
      "border-radius:10px",
      "background:rgba(24,24,24,.94)",
      "box-shadow:0 6px 24px rgba(0,0,0,.35)",
      "color:#f5f5f5",
      "font:14px/1.35 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif"
    ].join(";");

    var text = document.createElement("span");
    text.textContent = "关闭标签页后也接收微信私聊提醒";
    var button = document.createElement("button");
    button.type = "button";
    button.textContent = "启用提醒";
    button.style.cssText = [
      "appearance:none",
      "border:0",
      "border-radius:7px",
      "padding:7px 11px",
      "background:#07c160",
      "color:white",
      "font:600 14px/1.2 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif",
      "cursor:pointer",
      "white-space:nowrap"
    ].join(";");
    banner.appendChild(text);
    banner.appendChild(button);
    document.body.appendChild(banner);
    banner._wechatText = text;
    banner._wechatButton = button;
    return banner;
  }

  function showError(banner, error) {
    console.warn(TAG, error);
    banner._wechatText.textContent = error && error.message ? error.message : "微信提醒启用失败";
    banner._wechatButton.disabled = false;
    banner._wechatButton.textContent = "重试";
    banner._wechatButton.style.opacity = "1";
  }

  async function enableFromGesture(banner) {
    banner._wechatButton.disabled = true;
    banner._wechatButton.style.opacity = ".65";
    banner._wechatButton.textContent = "正在启用…";
    banner._wechatText.textContent = "正在连接微信消息提醒";
    try {
      var permission = Notification.permission;
      if (permission === "default") {
        permission = await Notification.requestPermission();
      }
      if (permission !== "granted") {
        throw new Error("Chrome 已阻止通知，请在站点设置中允许后重试");
      }
      var config = await getReadyConfig(true);
      var subscription = await ensureSubscription(config);
      await api("test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ endpoint: subscription.endpoint })
      });
      localStorage.setItem(ENABLED_KEY, "1");
      banner._wechatText.textContent = "微信提醒已启用，测试通知正在送达";
      banner._wechatButton.textContent = "已启用";
      removeBannerSoon(banner);
    } catch (error) {
      showError(banner, error);
    }
  }

  async function autoRepair() {
    try {
      var config = await getReadyConfig(false);
      await ensureSubscription(config);
      localStorage.setItem(ENABLED_KEY, "1");
    } catch (error) {
      var banner = createBanner();
      showError(banner, error);
      banner._wechatButton.onclick = function () { enableFromGesture(banner); };
    }
  }

  function initialize() {
    if (!supported()) {
      console.warn(TAG, "secure-context Web Push APIs are unavailable");
      return;
    }
    if (Notification.permission === "granted" && localStorage.getItem(ENABLED_KEY) === "1") {
      autoRepair();
      return;
    }
    var banner = createBanner();
    if (Notification.permission === "denied") {
      banner._wechatText.textContent = "Chrome 已阻止通知，请先在站点设置中允许";
      banner._wechatButton.textContent = "重试";
    }
    banner._wechatButton.onclick = function () { enableFromGesture(banner); };
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initialize, { once: true });
  } else {
    initialize();
  }
}());
