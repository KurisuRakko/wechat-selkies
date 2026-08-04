/*
 * Drag-and-drop bridge between the host desktop and WeChat inside the container.
 *
 * Two behaviours, neither of which upstream Selkies has:
 *
 *   IN   Drop a file on the stream and it is pasted into whatever WeChat input
 *        has focus. Images go onto the X clipboard as image/png (WeChat is a Qt
 *        app; Qt maps an image/png selection target onto application/x-qt-image,
 *        which is what QMimeData::hasImage() consumes, so Ctrl+V inlines the
 *        picture). Anything else goes on as text/uri-list pointing at the copy
 *        Selkies just uploaded, which Qt surfaces via QMimeData::hasUrls() so
 *        the paste attaches the file.
 *
 *   OUT  Drag a row out of the sidebar's file list onto the host desktop and it
 *        downloads, using Chromium's DownloadURL DataTransfer format. nginx
 *        already serves /config/Desktop at /files with Content-Disposition:
 *        attachment, so no server-side work is needed. Chromium only — Firefox
 *        has no equivalent.
 *
 * This runs as a plain (non-module) script alongside the bundle, the same way
 * src/universalTouchGamepad.js does, and talks to the stream only through the
 * already-exported window.webrtcInput.send(). Nothing in the minified bundle is
 * modified, so a base-image bump cannot silently break it — it either finds its
 * hooks or logs that it did not.
 *
 * The upload itself is still done by Selkies' own drop handler; this only adds
 * the clipboard write and the paste. So a dropped file always also lands in
 * /config/Desktop and stays reachable at /files.
 */
(function () {
  "use strict";

  var TAG = "[wechat-dragdrop]";

  // Selkies disables its own drop handling for read-only viewers; match that.
  if (String(location.hash).indexOf("shared") !== -1 ||
      String(location.hash).indexOf("player") !== -1) {
    return;
  }

  // xclip is spawned asynchronously on the server, so the selection is not
  // guaranteed to be in place the instant the cb message is sent. Give it a
  // moment before pressing Ctrl+V. Overridable for debugging.
  var PASTE_DELAY_MS = Number(window.WECHAT_DRAGDROP_PASTE_DELAY || 500);
  var UPLOAD_DIR = String(window.WECHAT_DRAGDROP_UPLOAD_DIR || "/config/Desktop");

  var XK_CONTROL_L = 65507;
  var XK_v = 118;

  var EXT_MIME = {
    png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", gif: "image/gif",
    webp: "image/webp", bmp: "image/bmp", svg: "image/svg+xml", ico: "image/x-icon",
    pdf: "application/pdf", txt: "text/plain", md: "text/markdown",
    zip: "application/zip", "7z": "application/x-7z-compressed",
    mp4: "video/mp4", mov: "video/quicktime", mp3: "audio/mpeg", wav: "audio/wav",
    doc: "application/msword", xls: "application/vnd.ms-excel",
    docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    pptx: "application/vnd.openxmlformats-officedocument.presentationml.presentation"
  };

  function mimeFor(name) {
    var m = /\.([^.]+)$/.exec(String(name).toLowerCase());
    return (m && EXT_MIME[m[1]]) || "application/octet-stream";
  }

  // btoa() cannot take a huge argument list, so chunk it.
  function bytesToBase64(buf) {
    var bytes = new Uint8Array(buf), out = "", CHUNK = 0x8000;
    for (var i = 0; i < bytes.length; i += CHUNK) {
      out += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
    }
    return btoa(out);
  }

  function utf8ToBase64(str) {
    return bytesToBase64(new TextEncoder().encode(str));
  }

  function send(msg) {
    var input = window.webrtcInput;
    if (!input || typeof input.send !== "function") {
      console.warn(TAG, "window.webrtcInput.send unavailable; dropping message");
      return false;
    }
    input.send(msg);
    return true;
  }

  function pressCtrlV() {
    // Same wire format the bundle's _sendKeyEvent produces.
    if (!send("kd," + XK_CONTROL_L)) return;
    send("kd," + XK_v);
    send("ku," + XK_v);
    send("ku," + XK_CONTROL_L);
  }

  // Re-encode to PNG so the advertised image/png target always matches the
  // bytes. A dropped JPEG/WebP announced as image/png would paste as garbage.
  function toPngBuffer(file) {
    return new Promise(function (resolve, reject) {
      var url = URL.createObjectURL(file);
      var img = new Image();
      img.onload = function () {
        try {
          var c = document.createElement("canvas");
          c.width = img.naturalWidth;
          c.height = img.naturalHeight;
          c.getContext("2d").drawImage(img, 0, 0);
          c.toBlob(function (blob) {
            URL.revokeObjectURL(url);
            if (!blob) { reject(new Error("canvas.toBlob returned null")); return; }
            blob.arrayBuffer().then(resolve, reject);
          }, "image/png");
        } catch (e) {
          URL.revokeObjectURL(url);
          reject(e);
        }
      };
      img.onerror = function () {
        URL.revokeObjectURL(url);
        reject(new Error("could not decode " + file.name));
      };
      img.src = url;
    });
  }

  function delay(ms) {
    return new Promise(function (r) { setTimeout(r, ms); });
  }

  function pasteImage(file) {
    return toPngBuffer(file).then(function (buf) {
      if (!send("cb,image/png," + bytesToBase64(buf))) return;
      console.log(TAG, "image on clipboard:", file.name, buf.byteLength, "bytes");
      return delay(PASTE_DELAY_MS).then(pressCtrlV);
    });
  }

  function pasteFileUri(name) {
    // The uploader keeps the dropped name verbatim under the upload dir.
    var uri = "file://" + UPLOAD_DIR + "/" + encodeURIComponent(name) + "\r\n";
    if (!send("cb,text/uri-list," + utf8ToBase64(uri))) return Promise.resolve();
    console.log(TAG, "uri-list on clipboard:", uri.trim());
    return delay(PASTE_DELAY_MS).then(pressCtrlV);
  }

  /* ---------------------------------------------------------------- drop in */

  // Files whose upload we are waiting on, keyed by the name the uploader will
  // report back in its fileUpload postMessage.
  var pendingUploads = Object.create(null);
  var chain = Promise.resolve();

  function queue(fn) {
    chain = chain.then(fn).catch(function (e) { console.warn(TAG, e); });
    return chain;
  }

  function onDrop(ev) {
    // Selkies' own handler already called preventDefault and is doing the
    // upload; do not interfere with it, just read the same files.
    var dt = ev.dataTransfer;
    if (!dt) return;
    // Must be read synchronously — the DataTransfer is neutered after this turn.
    var files = dt.files ? Array.prototype.slice.call(dt.files) : [];
    if (!files.length) return;

    files.forEach(function (file) {
      if (file.type && file.type.indexOf("image/") === 0) {
        // An image does not need the uploaded copy; paste it straight away.
        queue(function () { return pasteImage(file); });
      } else {
        // A file URI has to point at something that exists, so wait for the
        // upload Selkies is already performing to finish.
        pendingUploads[file.name] = true;
      }
    });
  }

  function onUploadMessage(ev) {
    if (ev.origin !== window.location.origin) return;
    var d = ev.data;
    if (!d || d.type !== "fileUpload" || !d.payload) return;
    var p = d.payload, name = p.fileName;
    if (!name || !pendingUploads[name]) return;

    if (p.status === "end") {
      delete pendingUploads[name];
      queue(function () { return pasteFileUri(name); });
    } else if (p.status === "error") {
      delete pendingUploads[name];
      console.warn(TAG, "upload failed, not pasting:", name, p.message);
    }
  }

  /* -------------------------------------------------------------- drag out */

  function decorateLink(a, doc) {
    if (a.dataset.wechatDragdrop) return;
    // fancyindex emits the real name in title= and a urlencoded href.
    var name = a.getAttribute("title") ||
               decodeURIComponent(a.getAttribute("href") || "");
    if (!name || name === "../" || /\/$/.test(name)) return;

    a.dataset.wechatDragdrop = "1";
    a.setAttribute("draggable", "true");
    a.addEventListener("dragstart", function (ev) {
      try {
        var abs = new URL(a.getAttribute("href"), doc.baseURI || location.href).href;
        // Chromium's format: mime:filename:absolute-url
        ev.dataTransfer.setData("DownloadURL", mimeFor(name) + ":" + name + ":" + abs);
        ev.dataTransfer.effectAllowed = "copy";
      } catch (e) {
        console.warn(TAG, "dragstart failed for", name, e);
      }
    });
  }

  function decorateIframe(iframe) {
    var doc;
    try {
      doc = iframe.contentDocument;
    } catch (e) {
      return; // cross-origin; nothing we can do
    }
    if (!doc || !doc.body) return;
    var links = doc.querySelectorAll("a[href]");
    for (var i = 0; i < links.length; i++) decorateLink(links[i], doc);
    if (links.length) console.log(TAG, "made", links.length, "file link(s) draggable");
  }

  function watchFileIframes() {
    function scan() {
      var frames = document.querySelectorAll('iframe[src*="files"]');
      for (var i = 0; i < frames.length; i++) {
        var f = frames[i];
        if (!f.dataset.wechatDragdropWatched) {
          f.dataset.wechatDragdropWatched = "1";
          f.addEventListener("load", function (e) { decorateIframe(e.target); });
        }
        decorateIframe(f);
      }
    }
    scan();
    // The modal is created on demand, and fancyindex navigation replaces the
    // document, so keep looking rather than binding once.
    new MutationObserver(scan).observe(document.documentElement, {
      childList: true, subtree: true
    });
    setInterval(scan, 2000);
  }

  /* ------------------------------------------------------ drop-in attachment */

  function attach() {
    var el = document.getElementById("overlayInput");
    if (!el) return false;
    if (el.dataset.wechatDragdrop) return true;
    el.dataset.wechatDragdrop = "1";
    // Non-capturing, so Selkies' own handler still runs and still uploads.
    el.addEventListener("drop", onDrop);
    window.addEventListener("message", onUploadMessage);
    console.log(TAG, "attached to #overlayInput");
    return true;
  }

  /* ------------------------------------------------- open URLs in this browser */

  // /usr/local/bin/xdg-open inside the container appends "<epoch_ms> <url>" here
  // instead of trying to open a browser it does not have. nginx already publishes
  // /config/Desktop at /files, and there is no dotfile deny rule, so this needs
  // no server-side change at all.
  var URL_QUEUE = "./files/.wechat-open-urls";
  var URL_POLL_MS = Number(window.WECHAT_URL_POLL_MS || 1000);
  // A link clicked while nobody was watching should not pop up hours later.
  var URL_MAX_AGE_MS = Number(window.WECHAT_URL_MAX_AGE_MS || 60000);
  // Cursor is persisted so a page reload does not replay the whole queue.
  var URL_CURSOR_KEY = "wechatDragdrop.urlCursor";

  function urlCursor() {
    var v = Number(localStorage.getItem(URL_CURSOR_KEY) || 0);
    return isFinite(v) ? v : 0;
  }

  function setUrlCursor(ts) {
    try { localStorage.setItem(URL_CURSOR_KEY, String(ts)); } catch (e) { /* private mode */ }
  }

  function showOpenPrompt(url) {
    // window.open outside a user gesture is popup-blocked. A real anchor the user
    // clicks is a fresh gesture and always allowed.
    var host;
    try { host = new URL(url).host; } catch (e) { host = url; }

    var bar = document.createElement("div");
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
    a.rel = "noopener noreferrer";
    a.textContent = "🔗 在浏览器打开 " + host;
    a.style.cssText = "color:#7ab8ff;text-decoration:none;font-weight:600";
    a.addEventListener("click", function () { bar.remove(); });

    var x = document.createElement("button");
    x.textContent = "✕";
    x.style.cssText =
      "background:none;border:none;color:#aaa;cursor:pointer;font-size:15px;line-height:1";
    x.addEventListener("click", function () { bar.remove(); });

    bar.appendChild(a);
    bar.appendChild(x);
    document.body.appendChild(bar);
    setTimeout(function () { bar.remove(); }, URL_MAX_AGE_MS);
  }

  function openForwardedUrl(url) {
    // Never hand window.open a scheme the container could use to run script in
    // this origin; the queue file is world-readable over /files.
    if (!/^(https?:|mailto:)/i.test(url)) {
      console.warn(TAG, "refusing to open non-http(s) url:", url);
      return;
    }
    var w = null;
    try { w = window.open(url, "_blank", "noopener,noreferrer"); } catch (e) { w = null; }
    if (w) {
      console.log(TAG, "opened", url);
    } else {
      console.log(TAG, "popup blocked, showing prompt for", url);
      showOpenPrompt(url);
    }
  }

  function pollUrlQueue() {
    if (document.hidden) return;   // a hidden tab cannot usefully open anything
    fetch(URL_QUEUE, { cache: "no-store" })
      .then(function (r) { return r.ok ? r.text() : null; })
      .then(function (text) {
        if (!text) return;         // 404 until the first link is clicked
        var cursor = urlCursor();
        var now = Date.now();
        var newest = cursor;
        text.split("\n").forEach(function (line) {
          var sp = line.indexOf(" ");
          if (sp <= 0) return;
          var ts = Number(line.slice(0, sp));
          var url = line.slice(sp + 1).trim();
          if (!isFinite(ts) || !url) return;
          if (ts > newest) newest = ts;
          if (ts <= cursor) return;              // already handled
          if (now - ts > URL_MAX_AGE_MS) return; // too old to be wanted
          openForwardedUrl(url);
        });
        if (newest > cursor) setUrlCursor(newest);
      })
      .catch(function () { /* nginx not ready, or offline */ });
  }

  function watchUrlQueue() {
    // Start the cursor at "now" so links queued long before this tab existed are
    // not opened on load. Stale entries are also age-filtered above.
    if (!localStorage.getItem(URL_CURSOR_KEY)) setUrlCursor(Date.now());
    setInterval(pollUrlQueue, URL_POLL_MS);
  }

  /* ------------------------------------------------------------------ setup */

  function boot() {
    var tries = 0;
    var t = setInterval(function () {
      tries++;
      var ready = attach() && window.webrtcInput;
      if (ready) {
        clearInterval(t);
        watchFileIframes();
        watchUrlQueue();
        console.log(TAG, "ready (paste delay " + PASTE_DELAY_MS + "ms, upload dir " + UPLOAD_DIR +
          ", url poll " + URL_POLL_MS + "ms)");
      } else if (tries > 120) {
        clearInterval(t);
        console.warn(TAG, "gave up waiting for #overlayInput / window.webrtcInput");
      }
    }, 500);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
