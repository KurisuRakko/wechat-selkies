/*
 * One visible quality control instead of eleven.
 *
 * Selkies' sidebar exposes encoder, framerate, CRF, bitrate, rate-control mode,
 * paint-over quality and more, all persisted per-URL in localStorage — and a
 * browser that once remembered "H264 (CPU) FullFrame, no rate control" keeps
 * re-imposing it on every reconnect, which is exactly how this deployment ended
 * up encoding whole 2400x1888 frames with CRF bouncing between 5 and 50. This
 * script puts four named presets in the top bar and leaves the sidebar intact
 * as the advanced view.
 *
 *   省流  12 fps   2 Mbps   静态 CRF 33
 *   流畅  24 fps   6 Mbps   静态 CRF 28   (default)
 *   高清  30 fps  12 Mbps   静态 CRF 23
 *   极致  60 fps  20 Mbps   静态 CRF 18
 *
 * Every page load also runs one background speed test and auto-selects a
 * preset from the measured Mbps/RTT. A manual click locks the session; a page
 * refresh clears that lock and tests again.
 *
 * All four use x264enc-striped (damage-based striping; plain "x264enc" is the
 * full-frame variant) with CBR rate control, so the bitrate figure is an actual
 * ceiling rather than a suggestion.
 *
 * Two ways in, both required:
 *
 *   * The Selkies settings live in localStorage under a per-URL prefix and are
 *     read once when the bundle initialises. This file is a classic script in
 *     <body> and therefore runs before the deferred module bundle, so seeding
 *     those keys synchronously at load is what makes the preset apply to the
 *     first connection.
 *   * Clicking a preset afterwards posts the same
 *     {type:"settings", settings:{…}} message the sidebar itself posts, which
 *     the bundle applies live and forwards to the server. No reload.
 *
 * The server clamps whatever it receives to the ranges in SELKIES_* env, so the
 * compose file has to allow 12-60 fps and 2-20 Mbps for these to survive. Static
 * scene quality is h264_paintover_crf (lower CRF = better), matching the key the
 * dashboard's "Static Region Optimization CRF" slider writes.
 *
 * The #wechat-topbar host element is shared with wechat-connection-status.js;
 * whichever script runs first creates it.
 */
(function () {
  "use strict";

  var TAG = "[wechat-quality-presets]";

  if (String(location.hash).indexOf("shared") !== -1 ||
      String(location.hash).indexOf("player") !== -1) {
    return;
  }
  if (window.wechatQualityPresetsInstalled) return;
  window.wechatQualityPresetsInstalled = true;

  var TOPBAR_ID = "wechat-topbar";
  var GROUP_ID = "wechat-quality-presets";
  var PRESET_KEY = "wechatQualityPreset";

  var ENCODER = "x264enc-striped";
  var RATE_CONTROL_MODE = "cbr";
  var SPEED_TEST_TIMEOUT_MS = 3000;
  // Dedicated 1 MiB same-origin payload created by the Dockerfile. It is large
  // enough for a stable measurement and is not part of the app's normal cache.
  var SPEED_TEST_RESOURCE = "wechat-speedtest.bin";

  var PRESETS = [
    { id: "datasaver", label: "省流", framerate: 12, bitrate: 2, staticCrf: 33 },
    { id: "smooth", label: "流畅", framerate: 24, bitrate: 6, staticCrf: 28 },
    { id: "hd", label: "高清", framerate: 30, bitrate: 12, staticCrf: 23 },
    { id: "max", label: "极致", framerate: 60, bitrate: 20, staticCrf: 18 }
  ];
  var DEFAULT_ID = "smooth";

  var ACTIVE_BG = "#07c160";
  var userLocked = false;

  function presetById(id) {
    for (var i = 0; i < PRESETS.length; i++) {
      if (PRESETS[i].id === id) return PRESETS[i];
    }
    return null;
  }

  // Verbatim from the bundle. Note the character class is a RANGE ".-_", not
  // three literals, so ":" and "/" survive — do not "fix" it or the prefix
  // stops matching the keys Selkies actually reads.
  function keyPrefix() {
    return window.location.href.split("#")[0].replace(/[^a-zA-Z0-9.-_]/g, "_");
  }

  function selkiesKey(name) {
    return keyPrefix() + "_" + name;
  }

  function settingsFor(preset) {
    return {
      encoder: ENCODER,
      framerate: preset.framerate,
      rate_control_mode: RATE_CONTROL_MODE,
      video_bitrate: preset.bitrate,
      h264_paintover_crf: preset.staticCrf
    };
  }

  // Written before the bundle boots so the very first connection already uses
  // the preset; the bundle rewrites the same keys when it applies a settings
  // message, so this never fights with it.
  function seed(preset) {
    var settings = settingsFor(preset);
    try {
      for (var name in settings) {
        if (Object.prototype.hasOwnProperty.call(settings, name)) {
          window.localStorage.setItem(selkiesKey(name), String(settings[name]));
        }
      }
    } catch (e) {
      console.warn(TAG, "could not persist Selkies settings", e);
    }
  }

  function readPreset() {
    var stored = null;
    try {
      stored = window.localStorage.getItem(PRESET_KEY);
    } catch (e) {
      stored = null;
    }
    return presetById(stored) || presetById(DEFAULT_ID);
  }

  function persistPreset(preset) {
    try {
      window.localStorage.setItem(PRESET_KEY, preset.id);
    } catch (e) {
      /* private mode */
    }
  }

  var current = readPreset();
  seed(current);
  persistPreset(current);

  function now() {
    if (window.performance && typeof window.performance.now === "function") {
      return window.performance.now();
    }
    return Date.now();
  }

  function withCacheBust(url) {
    var separator = url.indexOf("?") === -1 ? "?" : "&";
    return url.split("#")[0] + separator + "cachebust=" +
      Math.random().toString(36).slice(2) + Date.now();
  }

  function speedTestUrl() {
    return withCacheBust(new URL(SPEED_TEST_RESOURCE, window.location.href).href);
  }

  function measureDownload() {
    var start = now();
    return window.fetch(speedTestUrl(), { cache: "no-store" })
      .then(function (response) {
        if (!response.ok) throw new Error("speed test HTTP " + response.status);
        return response.arrayBuffer();
      })
      .then(function (buffer) {
        var durationMs = Math.max(1, now() - start);
        return {
          bytes: buffer.byteLength,
          mbps: (buffer.byteLength * 8) / durationMs / 1000
        };
      });
  }

  function measureRtt() {
    var start = now();
    var url = new URL(
      window.location.pathname + "?rtt=" + Math.random().toString(36).slice(2),
      window.location.href
    ).href;
    return window.fetch(url, { method: "HEAD", cache: "no-store" })
      .then(function () {
        return Math.max(1, now() - start);
      })
      .catch(function () {
        return null;
      });
  }

  function pickPreset(mbps, rtt) {
    var preset;
    if (mbps < 3) {
      preset = presetById("datasaver");
    } else if (mbps < 8) {
      preset = presetById("smooth");
    } else if (mbps < 15) {
      preset = presetById("hd");
    } else {
      preset = presetById("max");
    }
    if (rtt !== null && rtt > 150 && preset && preset.id !== "datasaver") {
      preset = presetById("smooth");
    }
    return preset;
  }

  function startSpeedTest() {
    if (typeof Promise === "undefined" || typeof window.fetch !== "function") return;

    var timeout = new Promise(function (resolve) {
      setTimeout(function () {
        resolve(null);
      }, SPEED_TEST_TIMEOUT_MS);
    });
    // Promise.resolve().then(...) also catches synchronous fetch throws.
    var measured = Promise.all([
      Promise.resolve().then(measureDownload),
      Promise.resolve().then(measureRtt)
    ])
      .then(function (results) {
        return {
          mbps: results[0].mbps,
          rtt: results[1]
        };
      })
      .catch(function () {
        return null;
      });

    Promise.race([measured, timeout])
      .then(function (result) {
        if (!result) {
          console.warn(TAG, "speed test timed out or failed; keeping current preset", current.id);
          return;
        }
        // Race guard: if the user clicked a preset before this resolved,
        // userLocked is already true and the automatic result must not win.
        if (userLocked) {
          console.log(TAG, "speed test finished but user already picked a preset; keeping", current.id);
          return;
        }
        var preset = pickPreset(result.mbps, result.rtt);
        if (!preset) return;
        console.log(
          TAG,
          "speed test",
          result.mbps.toFixed(2),
          "Mbps, RTT",
          result.rtt === null ? "n/a" : result.rtt.toFixed(0),
          "ms, selected",
          preset.id
        );
        select(preset);
      })
      .catch(function (error) {
        console.warn(TAG, "speed test failed; keeping current preset", current.id, error);
      });
  }

  /* ------------------------------------------------------------------ UI */

  var buttons = [];

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

  function paint() {
    for (var i = 0; i < buttons.length; i++) {
      var entry = buttons[i];
      var active = entry.preset.id === current.id;
      entry.node.style.background = active ? ACTIVE_BG : "transparent";
      entry.node.style.color = active ? "#fff" : "#ddd";
      entry.node.style.fontWeight = active ? "600" : "400";
      entry.node.setAttribute("aria-pressed", active ? "true" : "false");
    }
  }

  function select(preset) {
    if (!preset) return;
    current = preset;
    persistPreset(preset);
    seed(preset);
    paint();
    // Same channel the sidebar uses, so the change applies live.
    window.postMessage(
      { type: "settings", settings: settingsFor(preset) },
      window.location.origin
    );
    console.log(TAG, "applied", preset.id, settingsFor(preset));
  }

  function build() {
    if (document.getElementById(GROUP_ID)) return;

    var group = document.createElement("div");
    group.id = GROUP_ID;
    group.setAttribute("role", "group");
    group.setAttribute("aria-label", "画质");
    group.style.cssText = [
      "display:flex", "align-items:center", "gap:2px",
      "padding:2px", "border-radius:999px",
      "background:rgba(0,0,0,.65)",
      "font:12px system-ui,-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif",
      "user-select:none"
    ].join(";");

    PRESETS.forEach(function (preset) {
      var button = document.createElement("button");
      button.type = "button";
      button.textContent = preset.label;
      button.title = preset.framerate + " fps · " + preset.bitrate +
        " Mbps · 静态 CRF " + preset.staticCrf;
      button.style.cssText = [
        "appearance:none", "border:0", "border-radius:999px",
        "padding:3px 9px", "background:transparent", "color:#ddd",
        "cursor:pointer", "font:inherit", "line-height:1.4"
      ].join(";");
      button.addEventListener("click", function () {
        userLocked = true;
        select(preset);
      });
      group.appendChild(button);
      buttons.push({ preset: preset, node: button });
    });

    ensureTopbar().appendChild(group);
    paint();
    console.log(TAG, "installed with preset", current.id);
  }

  function boot() {
    var tries = 0;
    var timer = setInterval(function () {
      tries++;
      if (document.body) {
        clearInterval(timer);
        build();
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

  startSpeedTest();
})();
