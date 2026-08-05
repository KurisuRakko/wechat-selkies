/*
 * Drag-and-drop bridge between the host desktop and WeChat inside the container.
 *
 * Two behaviours, neither of which upstream Selkies has:
 *
 *   IN   Drop a file on the stream and it is uploaded by Selkies' own handler
 *        to /config/Desktop, then put into WeChat's input box. The paste is
 *        performed server-side by the wechat-history integration
 *        (wechat_history/attach.py, reached through the same loopback API the
 *        notification UI uses), because only something running next to WeChat
 *        can activate the window, verify focus, click the input box and verify
 *        focus again before touching the clipboard. This page can only fire a
 *        blind Ctrl+V at the stream, which is kept solely as a fallback.
 *
 *   OUT  Drag a row out of the sidebar's file list onto the host desktop and it
 *        downloads, using Chromium's DownloadURL DataTransfer format. nginx
 *        already serves /config/Desktop at /files with Content-Disposition:
 *        attachment, so no server-side work is needed. Chromium only — Firefox
 *        has no equivalent.
 *
 * History, because it constrains what this file may do: images used to be
 * base64-encoded here and pushed as ONE `cb,image/png,…` message. In
 * --mode=websockets that message shares the video socket, and websockets'
 * server default is max_size=1 MiB — so every screenshot-sized drop was
 * answered with close code 1009 and took the whole session down, recovered
 * only by the client's 5 s location.reload() poll. Images now take exactly the
 * same chunked upload path as every other file. Do not reintroduce a sender
 * that puts whole file contents in a single stream message.
 *
 * This runs as a plain (non-module) script alongside the bundle, the same way
 * src/universalTouchGamepad.js does, and talks to the stream only through the
 * already-exported window.webrtcInput.send(). Nothing in the minified bundle is
 * modified, so a base-image bump cannot silently break it — it either finds its
 * hooks or logs that it did not.
 *
 * The upload itself is still done by Selkies' own drop handler; this only adds
 * the attach request. So a dropped file always also lands in /config/Desktop
 * and stays reachable at /files, whatever happens afterwards.
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
  // moment before pressing Ctrl+V. Only used by the fallback path.
  var PASTE_DELAY_MS = Number(window.WECHAT_DRAGDROP_PASTE_DELAY || 500);
  var UPLOAD_DIR = String(window.WECHAT_DRAGDROP_UPLOAD_DIR || "/config/Desktop");

  // The server waits up to 40 s for the file to finish landing plus up to 15 s
  // for the shared draft lock; nginx gives the whole proxied request 60 s.
  var ATTACH_TIMEOUT_MS = Number(window.WECHAT_ATTACH_TIMEOUT_MS || 55000);
  // A drop whose upload never reports back must not stay in the table forever.
  var PENDING_TTL_MS = 180000;
  var PENDING_SWEEP_MS = 30000;
  var TOAST_MS = 5000;

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

  // Only a hint for the server: it still decides by magic number whether the
  // bytes can be inlined as an image, so a wrong guess degrades to a file
  // attachment rather than pasting garbage.
  function kindFor(file) {
    var type = String((file && file.type) || "");
    if (type.indexOf("image/") === 0) return "image";
    if (mimeFor(file && file.name).indexOf("image/") === 0) return "image";
    return "file";
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

  function delay(ms) {
    return new Promise(function (r) { setTimeout(r, ms); });
  }

  /* ------------------------------------------------------------------ toast */

  // Deliberately not .notification-container: that belongs to the bundle's own
  // upload progress list, which is repositioned by wechat-connection-status.js.
  function toast(text) {
    if (!document.body) return;
    var bar = document.createElement("div");
    bar.setAttribute("role", "status");
    bar.style.cssText = [
      "position:fixed", "bottom:16px", "right:20px", "z-index:2147483646",
      "max-width:min(80vw,420px)", "box-sizing:border-box",
      "padding:9px 13px", "border-radius:8px",
      "background:rgba(31,31,31,.94)", "color:#fff",
      "font:13px/1.4 system-ui,-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif",
      "box-shadow:0 4px 16px rgba(0,0,0,.45)",
      "word-break:break-all", "pointer-events:none"
    ].join(";");
    bar.textContent = text;
    document.body.appendChild(bar);
    setTimeout(function () {
      if (bar.parentNode) bar.parentNode.removeChild(bar);
    }, TOAST_MS);
  }

  /* --------------------------------------------------------- fallback paste */

  // Best-effort only: blind Ctrl+V into whatever has focus inside the
  // container. Used when the server-side attach endpoint is not there at all
  // (a build with INSTALL_WECHAT_HISTORY=false), or is unreachable.
  function fallbackPaste(name) {
    var uri = "file://" + UPLOAD_DIR + "/" + encodeURIComponent(name) + "\r\n";
    if (!send("cb,text/uri-list," + utf8ToBase64(uri))) return Promise.resolve();
    console.log(TAG, "uri-list on clipboard:", uri.trim());
    return delay(PASTE_DELAY_MS).then(pressCtrlV);
  }

  /* ---------------------------------------------------------- attach client */

  // Relative to the directory this page is served from, so a SUBFOLDER
  // deployment reaches the same nginx location the notification UI uses.
  function attachUrl() {
    return location.pathname.replace(/[^/]*$/, "") +
      "wechat-notifications/api/attach";
  }

  function attachViaApi(name, meta) {
    var controller = typeof AbortController === "function" ? new AbortController() : null;
    var timer = setTimeout(function () {
      if (controller) controller.abort();
    }, ATTACH_TIMEOUT_MS);
    var options = {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        fileName: name,
        size: Number(meta && meta.size) || 0,
        kind: (meta && meta.kind) === "image" ? "image" : "file"
      })
    };
    if (controller) options.signal = controller.signal;

    return fetch(attachUrl(), options).then(function (response) {
      if (response.ok) {
        toast("已放入微信聊天框: " + name);
        console.log(TAG, "attached", name);
        return;
      }
      // 409 means the server reached WeChat and refused for a reason it can
      // describe (upload still in flight, draft lock busy, window not
      // visible, focus lost). A blind Ctrl+V would not do better and could
      // paste into the wrong window, so report it instead of falling back.
      if (response.status === 409) {
        return response.json().catch(function () { return null; }).then(function (body) {
          var message = body && body.error && body.error.message;
          toast("未能放入聊天框（文件已在 Desktop）: " + (message || name));
          console.warn(TAG, "attach refused", name, body);
        });
      }
      throw new Error("attach API " + response.status);
    }).catch(function (error) {
      // No endpoint (a build without the history integration), a 5xx, or the
      // request never completed. Try the legacy blind paste.
      console.warn(TAG, "attach failed, falling back to blind paste:", name, error);
      return fallbackPaste(name);
    }).then(function () {
      clearTimeout(timer);
    }, function (error) {
      clearTimeout(timer);
      console.warn(TAG, error);
    });
  }

  /* ---------------------------------------------------------------- drop in */

  // Files whose upload we are waiting on, keyed by the name the uploader will
  // report back in its fileUpload postMessage.
  var pendingUploads = Object.create(null);
  // Top-level directory names already announced, so a folder with 200 files
  // produces one toast rather than 200.
  var announcedFolders = Object.create(null);
  var chain = Promise.resolve();

  function queue(fn) {
    chain = chain.then(fn).catch(function (e) { console.warn(TAG, e); });
    return chain;
  }

  // Without this, a drop whose upload never reports "end" (the tab was hidden,
  // the socket died mid-transfer) stays in the table for the life of the page.
  function sweepPending() {
    var now = Date.now();
    var name;
    for (name in pendingUploads) {
      if (now - pendingUploads[name].ts > PENDING_TTL_MS) delete pendingUploads[name];
    }
    for (name in announcedFolders) {
      if (now - announcedFolders[name] > PENDING_TTL_MS) delete announcedFolders[name];
    }
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
      // Images included: they go through the same chunked upload as
      // everything else. See the note at the top of this file.
      pendingUploads[file.name] = {
        size: Number(file.size) || 0,
        kind: kindFor(file),
        ts: Date.now()
      };
    });
  }

  function onUploadMessage(ev) {
    if (ev.origin !== window.location.origin) return;
    var d = ev.data;
    if (!d || d.type !== "fileUpload" || !d.payload) return;
    var p = d.payload, name = p.fileName;
    if (!name) return;

    // A folder drop reports each member as "<top>/…/<leaf>". Those land under
    // /config/Desktop/<top>/ rather than directly in it, so there is nothing
    // single-file attach can be pointed at.
    if (String(name).indexOf("/") !== -1) {
      if (p.status !== "end") return;
      var top = String(name).split("/")[0];
      delete pendingUploads[top];
      if (announcedFolders[top]) return;
      announcedFolders[top] = Date.now();
      toast("文件夹已上传到 Desktop/" + top + "（未自动放入聊天框）");
      return;
    }

    var meta = pendingUploads[name];
    if (!meta) return;

    if (p.status === "end") {
      delete pendingUploads[name];
      if (!meta.size && Number(p.fileSize) > 0) meta.size = Number(p.fileSize);
      queue(function () { return attachViaApi(name, meta); });
    } else if (p.status === "error") {
      delete pendingUploads[name];
      console.warn(TAG, "upload failed, not attaching:", name, p.message);
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
        setInterval(sweepPending, PENDING_SWEEP_MS);
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
