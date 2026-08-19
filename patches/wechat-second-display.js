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
 *
 * A sidebar settings card (Video Settings' neighbour) exposes two user
 * preferences, both plain localStorage keys read fresh on every use, never
 * cached: wechatSecondDisplayPopupMode ("popup" default, or "tab") controls
 * the window.open() features string in openSecondDisplay(), and
 * wechatSecondDisplayAutoOpen (on by default) controls whether tick()'s
 * success handler also calls maybeAutoOpen(), which auto-triggers
 * openSecondDisplay() once per "episode" -- see maybeAutoOpen() below for
 * the exact edge/episode rules. Auto-open reuses openSecondDisplay() as-is:
 * a blocked window.open() (the common case, since a poll callback carries no
 * user gesture) still falls back to the click-through link exactly as a
 * manual click would.
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
  var CARD_ID = "wechat-second-display-settings-card";
  var MODE_SELECT_ID = "wechatSecondDisplayModeSelect";
  var AUTO_OPEN_TOGGLE_ID = "wechatSecondDisplayAutoOpenToggle";
  var POPUP_MODE_KEY = "wechatSecondDisplayPopupMode";
  var AUTO_OPEN_KEY = "wechatSecondDisplayAutoOpen";
  var VIDEO_SETTINGS_SECTION_ID = "video-settings-content";
  var POPUP_FEATURES = "popup=yes,width=1280,height=800";

  var pollDelay = POLL_MIN_MS;
  var pollTimer = null;
  var closeCheckTimer = null;
  var secondaryHandle = null;
  var lastUnassignedCount = 0;
  var autoOpenAttemptedThisEpisode = false;

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
        maybeAutoOpen();
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
    var features = readPopupMode() === "tab" ? "" : POPUP_FEATURES;
    var handle = null;
    try {
      // See the file header for why noopener/noreferrer are not passed here.
      handle = window.open(url, WINDOW_NAME, features);
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

  /* --------------------------------------------------- settings card */

  // Copied verbatim from wechat-locked-settings.js: this project's own
  // convention is "copy shared helpers, do not call across injected-script
  // files" (see wechat-idle-saver.js's file header, point 1).
  function hasClass(el, className) {
    if (el.classList && typeof el.classList.contains === "function") {
      return el.classList.contains(className);
    }
    return (" " + String(el.className || "") + " ").indexOf(" " + className + " ") !== -1;
  }

  // Same [aria-controls] -> climb-to-.sidebar-section traversal
  // wechat-locked-settings.js's hideSection() uses, but returning the node
  // instead of hiding it.
  function findSection(ariaControls) {
    var header = null;
    try {
      header = document.querySelector('[aria-controls="' + ariaControls + '"]');
    } catch (e) {
      header = null;
    }
    if (!header) return null;
    var section = header.parentNode;
    while (section && !hasClass(section, "sidebar-section")) {
      section = section.parentNode;
    }
    return section || null;
  }

  function readPopupMode() {
    try {
      return window.localStorage.getItem(POPUP_MODE_KEY) === "tab" ? "tab" : "popup";
    } catch (e) {
      return "popup";
    }
  }

  function persistPopupMode(mode) {
    try {
      window.localStorage.setItem(POPUP_MODE_KEY, mode === "tab" ? "tab" : "popup");
    } catch (e) {
      /* private mode */
    }
  }

  // Same "missing key -> default true, otherwise strictly '1'" convention as
  // wechat-idle-saver.js's readEnabled()/persistEnabled().
  function readAutoOpen() {
    try {
      var stored = window.localStorage.getItem(AUTO_OPEN_KEY);
      return stored === null ? true : stored === "1";
    } catch (e) {
      return true;
    }
  }

  function persistAutoOpen(value) {
    try {
      window.localStorage.setItem(AUTO_OPEN_KEY, value ? "1" : "0");
    } catch (e) {
      /* private mode */
    }
  }

  function paintAutoOpenToggle(button, enabled) {
    button.setAttribute("aria-pressed", enabled ? "true" : "false");
    button.style.background = enabled ? "#3fb950" : "#8b949e";
    button.textContent = enabled ? "开" : "关";
  }

  // Idempotent: returns the existing card untouched if this document already
  // has one (mirrors wechat-quality-presets.js's build()).
  function buildCard() {
    var existing = document.getElementById(CARD_ID);
    if (existing) return existing;

    var card = document.createElement("div");
    card.id = CARD_ID;
    card.className = "sidebar-section";

    var header = document.createElement("div");
    header.className = "sidebar-section-header";
    var heading = document.createElement("h3");
    heading.textContent = "副屏窗口";
    header.appendChild(heading);
    card.appendChild(header);

    var modeRow = document.createElement("div");
    modeRow.className = "dev-setting-item";
    var modeLabel = document.createElement("label");
    modeLabel.setAttribute("for", MODE_SELECT_ID);
    modeLabel.textContent = "副屏打开方式";
    var modeSelect = document.createElement("select");
    modeSelect.id = MODE_SELECT_ID;
    modeSelect.style.cssText = [
      "background:#2a2a2a", "color:#fff", "border:1px solid #555",
      "border-radius:4px", "padding:4px 8px", "font:inherit"
    ].join(";");
    var popupOption = document.createElement("option");
    popupOption.value = "popup";
    popupOption.textContent = "弹出窗口";
    var tabOption = document.createElement("option");
    tabOption.value = "tab";
    tabOption.textContent = "标签页";
    modeSelect.appendChild(popupOption);
    modeSelect.appendChild(tabOption);
    modeSelect.value = readPopupMode();
    modeSelect.addEventListener("change", function () {
      persistPopupMode(modeSelect.value);
    });
    modeRow.appendChild(modeLabel);
    modeRow.appendChild(modeSelect);
    card.appendChild(modeRow);

    var autoRow = document.createElement("div");
    autoRow.className = "dev-setting-item";
    var autoLabel = document.createElement("label");
    autoLabel.setAttribute("for", AUTO_OPEN_TOGGLE_ID);
    autoLabel.textContent = "检测到子窗口自动打开副屏";
    var autoToggle = document.createElement("button");
    autoToggle.type = "button";
    autoToggle.id = AUTO_OPEN_TOGGLE_ID;
    autoToggle.style.cssText = [
      "appearance:none", "border:0", "border-radius:999px",
      "padding:4px 14px", "color:#fff", "cursor:pointer",
      "font:12px system-ui,-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif",
      "min-width:32px"
    ].join(";");
    paintAutoOpenToggle(autoToggle, readAutoOpen());
    autoToggle.addEventListener("click", function () {
      var next = !readAutoOpen();
      persistAutoOpen(next);
      paintAutoOpenToggle(autoToggle, next);
    });
    autoRow.appendChild(autoLabel);
    autoRow.appendChild(autoToggle);
    card.appendChild(autoRow);

    return card;
  }

  function insertCard(section) {
    if (document.getElementById(CARD_ID)) return;
    section.parentNode.insertBefore(buildCard(), section.nextSibling);
    console.log(TAG, "settings card installed");
  }

  // Same bounded-poll idiom used by this file's own boot(): waits for the
  // Video Settings section specifically, not just document.body, since the
  // sidebar can render slightly later.
  function bootCard() {
    var tries = 0;
    var timer = setInterval(function () {
      tries++;
      var section = findSection(VIDEO_SETTINGS_SECTION_ID);
      if (section) {
        clearInterval(timer);
        insertCard(section);
      } else if (tries > 120) {
        clearInterval(timer);
        console.warn(TAG, "gave up waiting for the video-settings section; settings card not installed");
      }
    }, 500);
  }

  /* --------------------------------------------------------- auto-open */

  // One automatic attempt per "episode" -- the stretch of time
  // unassigned_count stays above zero. The flag resets exactly when the
  // count drops back to zero, so the next 0 -> >0 edge gets a fresh
  // attempt. The very first poll after page load, if it already reports a
  // count above zero, is treated the same as a real edge (the flag starts
  // false) -- deliberate, not an oversight: a stray window already present
  // at load time should get the same one attempt as one that appears later.
  // openSecondDisplay() already knows how to fall back to a click-through
  // link when window.open() is popup-blocked, and already no-ops (focus
  // only) when a handle from a previous attempt or a manual click is still
  // open -- this function only decides WHEN to call it automatically.
  function maybeAutoOpen() {
    if (lastUnassignedCount === 0) {
      autoOpenAttemptedThisEpisode = false;
      return;
    }
    if (autoOpenAttemptedThisEpisode) return;
    autoOpenAttemptedThisEpisode = true;
    if (!readAutoOpen()) return;
    openSecondDisplay();
  }

  /* --------------------------------------------------------------- setup */

  function boot() {
    var tries = 0;
    var timer = setInterval(function () {
      tries++;
      if (document.body) {
        clearInterval(timer);
        tick();
        bootCard();
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
