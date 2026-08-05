/*
 * Live connection indicator in the top-right corner, plus a watchdog for the
 * failure mode this stream has no other answer to.
 *
 * Why it exists: in --mode=websockets a half-open connection is invisible.
 * The socket never fires close, so the bundle's own reconnect (a 5 s
 * location.reload() poll driven by onclose) never runs, the picture simply
 * stops updating and the page sits there looking fine. Over Tailscale that
 * happens often enough to matter.
 *
 * The liveness signal is the server's periodic network_stats push, which
 * upload-and-stats-fixes.py forwards once a second. Two rules keep it honest:
 *
 *   * Freshness is measured locally. The payload's `timestamp` is an ISO string
 *     from the server's clock, so it is compared for *change* only and the age
 *     is counted from the local performance.now() at which it last changed. A
 *     container/browser clock skew therefore cannot fake a healthy or a dead
 *     link.
 *   * window.fps is deliberately not displayed. A static screen legitimately
 *     encodes 0 fps, which reads as "broken" to a user who is simply not
 *     moving the mouse.
 *
 * The reload watchdog is rate-limited in sessionStorage rather than trusted:
 * if reloading three times in ten minutes has not fixed it, reloading a fourth
 * time will not either, and a reload loop would destroy any unsent draft in
 * WeChat. It also stands down while a file upload is in progress and while the
 * tab is hidden, because background throttling stalls the stats push on the
 * client side without anything being wrong with the link.
 *
 * The #wechat-topbar host element is shared with wechat-quality-presets.js;
 * whichever script runs first creates it.
 */
(function () {
  "use strict";

  var TAG = "[wechat-connection-status]";

  // Read-only viewers get no controls anywhere else either.
  if (String(location.hash).indexOf("shared") !== -1 ||
      String(location.hash).indexOf("player") !== -1) {
    return;
  }
  if (window.wechatConnectionStatusInstalled) return;
  window.wechatConnectionStatusInstalled = true;

  var TICK_MS = 500;
  var FRESH_MS = 6000;    // stats push is 1 s; six missed pushes is a hiccup
  var STALE_MS = 15000;   // ping_timeout is 20 s, so by now something is wrong
  var RELOAD_AFTER_MS = 20000;
  var UPLOAD_GRACE_MS = 15000;
  var RELOAD_WINDOW_MS = 600000;
  var RELOAD_LIMIT = 3;
  var RELOAD_DELAY_MS = 500;
  var RELOAD_KEY = "wechatConnStatus.reloads";

  var GREEN = "#3fb950";
  var AMBER = "#d29922";
  var RED = "#f85149";

  var TOPBAR_ID = "wechat-topbar";
  var STYLE_ID = "wechat-topbar-style";
  var PILL_ID = "wechat-connection-pill";

  var nowMs = (window.performance && typeof performance.now === "function")
    ? function () { return performance.now(); }
    : function () { return Date.now(); };

  var lastStamp = null;
  var lastStats = null;
  var lastChangeAt = 0;
  var everSeen = false;
  // Far enough in the past that the upload grace period is not active on load.
  var lastUploadAt = -1e12;
  var reloading = false;
  var dot = null;
  var label = null;
  var rendered = "";

  /* ------------------------------------------------------------- top bar */

  // Also created by wechat-quality-presets.js. Both are idempotent, so the
  // order the two scripts are injected in does not matter.
  function ensureTopbar() {
    var bar = document.getElementById(TOPBAR_ID);
    if (bar) return bar;
    bar = document.createElement("div");
    bar.id = TOPBAR_ID;
    bar.style.cssText = [
      "position:fixed", "top:8px", "right:20px", "z-index:1051",
      "display:flex", "align-items:center", "gap:8px",
      "pointer-events:auto"
    ].join(";");
    document.body.appendChild(bar);
    return bar;
  }

  // The bundle's upload progress list is anchored at the very top right, which
  // is now occupied. Nudging it down is a one-rule stylesheet rather than a
  // patch inside 666 KB of minified JS.
  function ensureStyle() {
    if (document.getElementById(STYLE_ID)) return;
    var style = document.createElement("style");
    style.id = STYLE_ID;
    style.textContent = ".notification-container{top:64px !important;}";
    (document.head || document.body).appendChild(style);
  }

  function buildPill() {
    var existing = document.getElementById(PILL_ID);
    if (existing) return existing;
    var pill = document.createElement("div");
    pill.id = PILL_ID;
    pill.setAttribute("role", "status");
    pill.style.cssText = [
      "display:flex", "align-items:center", "gap:6px",
      "padding:4px 10px", "border-radius:999px",
      "background:rgba(0,0,0,.65)", "color:#fff",
      "font:12px system-ui,-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif",
      "white-space:nowrap", "user-select:none"
    ].join(";");

    dot = document.createElement("span");
    dot.style.cssText = [
      "width:8px", "height:8px", "border-radius:50%",
      "background:" + AMBER, "flex:0 0 auto"
    ].join(";");

    label = document.createElement("span");
    label.textContent = "连接中…";

    pill.appendChild(dot);
    pill.appendChild(label);
    ensureTopbar().appendChild(pill);
    return pill;
  }

  function render(colour, text) {
    if (!dot || !label) return;
    var key = colour + "|" + text;
    if (key === rendered) return;
    rendered = key;
    dot.style.background = colour;
    label.textContent = text;
  }

  /* -------------------------------------------------------------- reload */

  function readReloads() {
    try {
      var raw = window.sessionStorage.getItem(RELOAD_KEY);
      var parsed = raw ? JSON.parse(raw) : [];
      if (!Array.isArray(parsed)) return [];
      return parsed.filter(function (value) { return typeof value === "number"; });
    } catch (e) {
      return [];
    }
  }

  function writeReloads(values) {
    try {
      window.sessionStorage.setItem(RELOAD_KEY, JSON.stringify(values));
    } catch (e) {
      /* private mode, or storage disabled */
    }
  }

  function maybeReload(age) {
    if (reloading) return;
    // A hidden tab is throttled by the browser, so a stalled stats push says
    // nothing about the link.
    if (document.hidden) return;
    // An upload saturates the same socket the stats ride on. Reloading here
    // would abandon the transfer for no reason.
    if (nowMs() - lastUploadAt < UPLOAD_GRACE_MS) return;
    if (age < RELOAD_AFTER_MS) return;

    var cutoff = Date.now() - RELOAD_WINDOW_MS;
    var history = readReloads().filter(function (value) { return value > cutoff; });
    if (history.length >= RELOAD_LIMIT) {
      render(RED, "连接已断开，请手动刷新");
      return;
    }

    reloading = true;
    history.push(Date.now());
    writeReloads(history);
    render(RED, "连接已卡死，正在刷新…");
    console.warn(TAG, "no server stats for " + Math.round(age) + "ms; reloading");
    setTimeout(function () { location.reload(); }, RELOAD_DELAY_MS);
  }

  /* ---------------------------------------------------------------- tick */

  function describe(stats) {
    var parts = [];
    var latency = stats && Number(stats.latency_ms);
    var bandwidth = stats && Number(stats.bandwidth_mbps);
    if (isFinite(latency) && latency >= 0) parts.push(Math.round(latency) + "ms");
    if (isFinite(bandwidth) && bandwidth >= 0) {
      parts.push("↓" + (bandwidth >= 10 ? Math.round(bandwidth) : bandwidth.toFixed(1)) + "Mbps");
    }
    return parts.length ? parts.join(" ") : "已连接";
  }

  function tick() {
    var stats = window.network_stats;
    if (stats && stats.timestamp !== lastStamp) {
      lastStamp = stats.timestamp;
      lastStats = stats;
      lastChangeAt = nowMs();
      everSeen = true;
    }

    var nav = window.navigator;
    if (nav && nav.onLine === false) {
      render(RED, "网络离线");
      return;
    }
    if (!everSeen) {
      render(AMBER, "连接中…");
      return;
    }

    var age = nowMs() - lastChangeAt;
    if (age < FRESH_MS) {
      render(GREEN, describe(lastStats));
      return;
    }
    if (age < STALE_MS) {
      render(AMBER, "网络卡顿");
      return;
    }
    render(RED, "连接中断");
    maybeReload(age);
  }

  /* --------------------------------------------------------------- setup */

  function install() {
    ensureStyle();
    buildPill();

    window.addEventListener("message", function (event) {
      if (event.origin !== window.location.origin) return;
      var data = event.data;
      if (data && data.type === "fileUpload") lastUploadAt = nowMs();
    });

    window.addEventListener("online", tick);
    window.addEventListener("offline", tick);
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) return;
      // Coming back from a throttled background tab, the first stats push can
      // be seconds away through no fault of the link. Start from "fresh" so a
      // returning user is not greeted by a reload.
      lastChangeAt = Math.max(lastChangeAt, nowMs() - FRESH_MS);
      tick();
    });

    setInterval(tick, TICK_MS);
    tick();
    console.log(TAG, "installed");
  }

  function boot() {
    var tries = 0;
    var timer = setInterval(function () {
      tries++;
      if (document.body) {
        clearInterval(timer);
        install();
      } else if (tries > 120) {
        clearInterval(timer);
        console.warn(TAG, "gave up waiting for document.body");
      }
    }, 500);
  }

  if (document.readyState === "loading" && !document.body) {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
