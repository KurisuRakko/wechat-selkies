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
 *   流畅  15 fps   4 Mbps
 *   标清  24 fps   8 Mbps   (default)
 *   高清  45 fps  12 Mbps
 *   超清  90 fps  24 Mbps
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
 * compose file has to allow 15-90 fps and 4-24 Mbps for these to survive.
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

  var PRESETS = [
    { id: "smooth", label: "流畅", framerate: 15, bitrate: 4 },
    { id: "sd", label: "标清", framerate: 24, bitrate: 8 },
    { id: "hd", label: "高清", framerate: 45, bitrate: 12 },
    { id: "uhd", label: "超清", framerate: 90, bitrate: 24 }
  ];
  var DEFAULT_ID = "sd";

  var ACTIVE_BG = "#07c160";

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
      video_bitrate: preset.bitrate
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
      button.title = preset.framerate + " fps · " + preset.bitrate + " Mbps";
      button.style.cssText = [
        "appearance:none", "border:0", "border-radius:999px",
        "padding:3px 9px", "background:transparent", "color:#ddd",
        "cursor:pointer", "font:inherit", "line-height:1.4"
      ].join(";");
      button.addEventListener("click", function () { select(preset); });
      group.appendChild(button);
      buttons.push({ preset: preset, node: button });
    });

    var hint = document.createElement("span");
    hint.textContent = "更多设置见左侧边栏";
    hint.style.cssText = [
      "padding:0 8px 0 4px", "color:#9a9a9a", "font-size:11px",
      "white-space:nowrap"
    ].join(";");
    group.appendChild(hint);

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
})();
