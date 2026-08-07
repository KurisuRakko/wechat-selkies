/*
 * Drag a file out of a WeChat chat and download it in this browser.
 *
 * The whole drag happens inside the remote X11 session — the browser only
 * forwards pointer events — so HTML5 dragover/drop never fire here and this
 * script cannot "catch" anything. The thing that actually receives the file is
 * an XDND window that /scripts/wechat/wechat-export-drop.py maps over the
 * remote screen's top-right corner for exactly as long as a drag is running.
 * Everything below is the visible half of that: a drop zone drawn where the
 * quality preset bar normally sits, driven by server-sent events.
 *
 *   drag-start     hide #wechat-quality-presets, show the drop zone
 *   drag-end       put the preset bar back
 *   file-exported  click a hidden a[download] to pull the file down
 *
 * The zone rectangle comes from the helper in remote device pixels. Selkies
 * sizes the remote screen to "CSS viewport x devicePixelRatio", so dividing by
 * (reported screen width / window.innerWidth) converts it to CSS pixels without
 * either side needing to know the other's scaling — and the hint then lands
 * exactly on the window that will accept the drop, not merely near it.
 *
 * A lost drag-end would otherwise hide the preset bar forever, so the zone also
 * tears itself down after RESTORE_TIMEOUT_MS. Missing hooks degrade to a log:
 * this file is loaded beside the bundle like the other wechat-*.js patches and
 * must never be able to break the page.
 */
(function () {
  "use strict";

  var TAG = "[wechat-desktop-export]";

  // Read-only viewers cannot drive the remote desktop, so there is no drag for
  // them to see. Same guard the sibling scripts use.
  if (String(location.hash).indexOf("shared") !== -1 ||
      String(location.hash).indexOf("player") !== -1) {
    return;
  }
  if (window.wechatDesktopExportInstalled) return;
  window.wechatDesktopExportInstalled = true;

  var ZONE_ID = "wechat-export-dropzone";
  var PRESETS_ID = "wechat-quality-presets";
  var API_PATH = "wechat-export/";

  // The helper caps a drag at 120 s, but a dropped SSE connection could swallow
  // drag-end entirely; never leave the preset bar hidden for longer than this.
  var RESTORE_TIMEOUT_MS = Number(window.WECHAT_EXPORT_RESTORE_TIMEOUT_MS || 10000);
  // Chrome drops programmatic downloads fired in the same tick, so a multi-file
  // drop is spread out.
  var DOWNLOAD_GAP_MS = 350;

  var ACCENT = "#07c160";

  // The directory this page is served from. Both the event stream and the
  // download URLs the helper reports are relative to it, so a SUBFOLDER
  // deployment reaches the same nginx location without any configuration.
  function pageBase() {
    return location.pathname.replace(/[^/]*$/, "");
  }

  function apiBase() {
    return pageBase() + API_PATH;
  }

  /* ------------------------------------------------------------------ zone */

  var zoneNode = null;
  var restoreTimer = null;
  var hiddenPresets = null;

  // Remote pixels -> CSS pixels. The remote screen is the viewport times the
  // device pixel ratio; deriving the factor from the two widths also absorbs
  // the encoder's alignment rounding.
  function cssScale(screen) {
    var viewport = window.innerWidth ||
      (document.documentElement && document.documentElement.clientWidth) || 0;
    var width = screen && Number(screen.w);
    if (!viewport || !width) return 1;
    var scale = width / viewport;
    return (isFinite(scale) && scale > 0.1 && scale < 8) ? scale : 1;
  }

  function hidePresets() {
    var group = document.getElementById(PRESETS_ID);
    if (!group) {
      console.warn(TAG, "#" + PRESETS_ID + " not found; showing the drop zone anyway");
      return;
    }
    if (hiddenPresets) return;
    hiddenPresets = { node: group, display: group.style.display };
    group.style.display = "none";
  }

  function restorePresets() {
    if (!hiddenPresets) return;
    hiddenPresets.node.style.display = hiddenPresets.display;
    hiddenPresets = null;
  }

  function buildZone() {
    var zone = document.createElement("div");
    zone.id = ZONE_ID;
    zone.setAttribute("role", "status");
    zone.style.cssText = [
      "position:fixed",
      // One below #wechat-topbar (1051) so the connection pill stays readable
      // on top of the zone rather than being covered by it.
      "z-index:1050",
      "box-sizing:border-box",
      "display:flex", "flex-direction:column",
      "align-items:center", "justify-content:center", "gap:6px",
      "border:2px dashed " + ACCENT,
      "border-radius:4px",
      "background:rgba(7,193,96,.14)",
      "box-shadow:0 4px 5px rgba(0,0,0,.2),0 1px 10px rgba(0,0,0,.12)",
      "color:#fff", "text-align:center",
      "font:500 14px/1.4 system-ui,-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif",
      "text-shadow:0 1px 3px rgba(0,0,0,.7)",
      "letter-spacing:.02em",
      // The real drop target is the remote window underneath; this must never
      // swallow a pointer event that belongs to the stream.
      "pointer-events:none",
      "user-select:none"
    ].join(";");

    var icon = document.createElement("div");
    icon.textContent = "⤓";
    icon.style.cssText = "font-size:26px;line-height:1";

    var label = document.createElement("div");
    label.textContent = "拖到这里下载";

    zone.appendChild(icon);
    zone.appendChild(label);
    return zone;
  }

  function showZone(payload) {
    if (!document.body) {
      console.warn(TAG, "no document.body yet; ignoring drag-start");
      return;
    }
    var rect = payload && payload.zone;
    if (!rect) {
      console.warn(TAG, "drag-start without a zone rectangle", payload);
      return;
    }
    var scale = cssScale(payload.screen);
    if (!zoneNode) {
      zoneNode = buildZone();
      document.body.appendChild(zoneNode);
    }
    zoneNode.style.left = (Number(rect.x) / scale) + "px";
    zoneNode.style.top = (Number(rect.y) / scale) + "px";
    zoneNode.style.width = (Number(rect.w) / scale) + "px";
    zoneNode.style.height = (Number(rect.h) / scale) + "px";
    zoneNode.style.display = "flex";

    hidePresets();

    if (restoreTimer) clearTimeout(restoreTimer);
    restoreTimer = setTimeout(function () {
      console.warn(TAG, "no drag-end within " + RESTORE_TIMEOUT_MS + "ms; restoring");
      hideZone();
    }, RESTORE_TIMEOUT_MS);
  }

  function hideZone() {
    if (restoreTimer) {
      clearTimeout(restoreTimer);
      restoreTimer = null;
    }
    if (zoneNode) zoneNode.style.display = "none";
    restorePresets();
  }

  /* -------------------------------------------------------------- download */

  var downloadQueue = Promise.resolve();

  function download(payload) {
    var url = payload && payload.url;
    var name = (payload && payload.name) || "";
    if (!url) {
      console.warn(TAG, "file-exported without a url", payload);
      return;
    }
    var href = pageBase() + url;
    downloadQueue = downloadQueue.then(function () {
      var link = document.createElement("a");
      link.href = href;
      link.download = name;
      link.rel = "noopener";
      link.style.display = "none";
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      console.log(TAG, "downloading", name, href);
      return new Promise(function (resolve) {
        setTimeout(resolve, DOWNLOAD_GAP_MS);
      });
    });
  }

  /* ------------------------------------------------------------------ wire */

  function connect() {
    var source = new EventSource(apiBase() + "events");

    source.addEventListener("hello", function (event) {
      // A tab that connects mid-drag still has to draw the zone, and one that
      // reconnects after a drag ended has to drop it.
      var payload = parse(event);
      if (payload && payload.dragging) showZone(payload);
      else hideZone();
    });
    source.addEventListener("drag-start", function (event) {
      showZone(parse(event));
    });
    source.addEventListener("drag-end", function () {
      hideZone();
    });
    source.addEventListener("file-exported", function (event) {
      download(parse(event));
    });
    source.addEventListener("open", function () {
      console.log(TAG, "connected to", apiBase() + "events");
    });
    source.addEventListener("error", function () {
      // EventSource reconnects on its own; only make sure a drag that was in
      // flight when the stream died does not leave the preset bar hidden.
      hideZone();
    });
  }

  function parse(event) {
    try {
      return JSON.parse(event.data);
    } catch (error) {
      console.warn(TAG, "bad event payload", event && event.data, error);
      return null;
    }
  }

  function boot() {
    if (typeof window.EventSource !== "function") {
      console.warn(TAG, "EventSource unavailable; drag-out export disabled");
      return;
    }
    connect();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, { once: true });
  } else {
    boot();
  }
})();
