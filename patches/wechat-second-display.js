/*
 * Prompt bar for the second-display window manager
 * (root/scripts/second_display). The daemon only reshuffles windows once a
 * second selkies browser window exists; this script's only job is telling
 * the user one would be useful, and opening it on an actual click.
 *
 * Two hash-based modes, checked once at load:
 *
 *   #display2   This tab IS the secondary display the user just opened
 *               (selkies-core.js itself reads this hash to join as
 *               displayId "display2"). It has nothing to prompt about, so
 *               it returns immediately — passive mode.
 *   otherwise   The normal/primary tab: poll the daemon's status endpoint
 *               and show a prompt bar while there are movable windows with
 *               nowhere to go.
 *
 * openSecondDisplay() deliberately does NOT pass "noopener"/"noreferrer" to
 * window.open(), unlike the external-link path in wechat-dragdrop.js. Both
 * flags make window.open() return null in every current browser (severing
 * the caller's reference is the whole point of noopener) — fine for a link
 * to an arbitrary, potentially untrusted URL, but it would silently break
 * the one thing this feature needs: holding onto the popup to focus a
 * reused window and to notice when the user closes it. The target here is
 * always this same page's own origin (location.href with the hash
 * replaced), never attacker-influenced input, so there is no
 * reverse-tabnabbing concern to trade away.
 */
(function () {
  "use strict";

  var TAG = "[wechat-second-display]";

  // Read-only viewers get no controls anywhere else either.
  if (String(location.hash).indexOf("shared") !== -1 ||
      String(location.hash).indexOf("player") !== -1) {
    return;
  }
  // This tab is itself the secondary display: nothing to prompt about.
  if (String(location.hash).indexOf("display2") !== -1) {
    return;
  }
  if (window.wechatSecondDisplayInstalled) return;
  window.wechatSecondDisplayInstalled = true;

  var STATUS_URL = "./wechat-second-display/api/status"; // relative: respects SUBFOLDER
  var POLL_MIN_MS = 5000;
  var POLL_MAX_MS = 30000;
  var CLOSED_CHECK_MS = 2000;
  var PROMPT_ID = "wechat-second-display-prompt";
  var WINDOW_NAME = "wechat-second-display";

  var pollDelay = POLL_MIN_MS;
  var pollTimer = null;
  var closeCheckTimer = null;
  var secondaryHandle = null;
  var lastUnassignedCount = 0;

  /* ------------------------------------------------------------- polling */

  function tick() {
    fetch(STATUS_URL, { cache: "no-store" })
      .then(function (response) {
        if (!response.ok) throw new Error("status " + response.status);
        return response.json();
      })
      .then(function (data) {
        pollDelay = POLL_MIN_MS;
        lastUnassignedCount = Number(data && data.unassigned_count) || 0;
        render();
      })
      .catch(function (error) {
        pollDelay = Math.min(pollDelay * 2, POLL_MAX_MS);
        console.warn(TAG, "status unavailable, backing off to " + pollDelay + "ms:", error);
      })
      .then(function () {
        pollTimer = setTimeout(tick, pollDelay);
      });
  }

  function render() {
    if (lastUnassignedCount > 0) {
      showPrompt(lastUnassignedCount);
    } else {
      hidePrompt();
    }
  }

  /* --------------------------------------------------------- prompt bar */

  // Visual style copied from wechat-dragdrop.js's showOpenPrompt: a dark
  // semi-transparent bar centered at the top, an action + a "✕" dismiss.
  function showPrompt(count) {
    if (!document.body) return;
    var existing = document.getElementById(PROMPT_ID);
    if (existing) {
      var button = existing.querySelector("button[data-action]");
      if (button) {
        button.textContent = "检测到 " + count + " 个可弹出的微信窗口 · 点击在副屏打开";
        return;
      }
      // Whatever currently occupies PROMPT_ID is not the normal prompt bar
      // (the popup-blocked fallback link from showFallbackPrompt() shares
      // the same id and has no data-action button) — replace it instead of
      // touching a node that was never there.
      existing.remove();
    }

    var bar = document.createElement("div");
    bar.id = PROMPT_ID;
    bar.style.cssText = [
      "position:fixed", "top:0", "left:50%", "transform:translateX(-50%)",
      "z-index:2147483647", "max-width:90vw", "box-sizing:border-box",
      "margin:8px", "padding:10px 14px", "border-radius:8px",
      "background:#1f1f1f", "color:#fff", "font:14px system-ui,sans-serif",
      "box-shadow:0 4px 16px rgba(0,0,0,.45)", "display:flex",
      "align-items:center", "gap:12px"
    ].join(";");

    var action = document.createElement("button");
    action.setAttribute("data-action", "open");
    action.textContent = "检测到 " + count + " 个可弹出的微信窗口 · 点击在副屏打开";
    action.style.cssText =
      "background:none;border:none;color:#7ab8ff;font-weight:600;font:inherit;cursor:pointer;padding:0";
    action.addEventListener("click", openSecondDisplay);

    var dismiss = document.createElement("button");
    dismiss.textContent = "✕";
    dismiss.style.cssText =
      "background:none;border:none;color:#aaa;cursor:pointer;font-size:15px;line-height:1";
    dismiss.addEventListener("click", function () { bar.remove(); });

    bar.appendChild(action);
    bar.appendChild(dismiss);
    document.body.appendChild(bar);
  }

  function hidePrompt() {
    var existing = document.getElementById(PROMPT_ID);
    if (existing) existing.remove();
  }

  // Degraded path when the popup blocker refuses window.open(): a real
  // anchor click is always a fresh user gesture and is never blocked, same
  // reasoning as wechat-dragdrop.js's own showOpenPrompt. This path cannot
  // offer reuse/focus/close-tracking — it is a plain "open in a new tab".
  function showFallbackPrompt(url) {
    hidePrompt();
    if (!document.body) return;
    var bar = document.createElement("div");
    bar.id = PROMPT_ID;
    bar.style.cssText = [
      "position:fixed", "top:0", "left:50%", "transform:translateX(-50%)",
      "z-index:2147483647", "max-width:90vw", "box-sizing:border-box",
      "margin:8px", "padding:10px 14px", "border-radius:8px",
      "background:#1f1f1f", "color:#fff", "font:14px system-ui,sans-serif",
      "box-shadow:0 4px 16px rgba(0,0,0,.45)", "display:flex",
      "align-items:center", "gap:12px"
    ].join(";");

    var a = document.createElement("a");
    a.href = url;
    a.target = "_blank";
    a.rel = "noopener noreferrer"; // no reuse/tracking needed on this fallback path
    a.textContent = "🔗 在浏览器打开副屏窗口";
    a.style.cssText = "color:#7ab8ff;text-decoration:none;font-weight:600";
    a.addEventListener("click", function () { bar.remove(); });

    var dismiss = document.createElement("button");
    dismiss.textContent = "✕";
    dismiss.style.cssText =
      "background:none;border:none;color:#aaa;cursor:pointer;font-size:15px;line-height:1";
    dismiss.addEventListener("click", function () { bar.remove(); });

    bar.appendChild(a);
    bar.appendChild(dismiss);
    document.body.appendChild(bar);
  }

  /* ------------------------------------------------------ opening it */

  function openSecondDisplay() {
    if (secondaryHandle && !secondaryHandle.closed) {
      secondaryHandle.focus();
      return;
    }

    var url = location.href.split("#")[0] + "#display2";
    var handle = null;
    try {
      // See the file header for why noopener/noreferrer are not passed here.
      handle = window.open(url, WINDOW_NAME);
    } catch (e) {
      handle = null;
    }

    if (handle) {
      secondaryHandle = handle;
      hidePrompt();
      watchSecondaryHandle();
      console.log(TAG, "opened second display window");
    } else {
      console.warn(TAG, "window.open was blocked, falling back to a click-through link");
      showFallbackPrompt(url);
    }
  }

  function watchSecondaryHandle() {
    if (closeCheckTimer) return;
    closeCheckTimer = setInterval(function () {
      if (!secondaryHandle || secondaryHandle.closed) {
        secondaryHandle = null;
        clearInterval(closeCheckTimer);
        closeCheckTimer = null;
        // Re-render immediately from the last known count instead of
        // waiting up to POLL_MIN_MS for the next scheduled poll.
        render();
      }
    }, CLOSED_CHECK_MS);
  }

  /* --------------------------------------------------------------- setup */

  function boot() {
    var tries = 0;
    var timer = setInterval(function () {
      tries++;
      if (document.body) {
        clearInterval(timer);
        tick();
        console.log(TAG, "installed");
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
