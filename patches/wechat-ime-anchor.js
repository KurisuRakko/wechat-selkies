/*
 * Anchor the host IME candidate window to the last click on the stream.
 *
 * Selkies uses one transparent, full-screen input for both mouse events and
 * composition. Padding that input does not reliably move the native caret on
 * macOS Chromium: the candidate window is still laid out against the large
 * form control. Keep that element untouched for pointer math and use a real,
 * one-line textarea at the click point as the IME focus target instead.
 *
 * Keyboard events are already captured on window by Selkies. Only the three
 * composition events need forwarding from the proxy to window.webrtcInput.
 */
(function () {
  "use strict";

  var TAG = "[wechat-ime-anchor]";
  var PROXY_ID = "wechatImeProxy";
  var PROXY_WIDTH_PX = 1;
  var LINE_PX = 20;
  var DEFAULT_FRAC_X = 0.4;
  var DEFAULT_FRAC_Y = 0.85;

  if (String(location.hash).indexOf("shared") !== -1 ||
      String(location.hash).indexOf("player") !== -1) {
    return;
  }

  if (window.__wechatImeAnchorInstalled) return;
  window.__wechatImeAnchorInstalled = true;

  var overlay = null;
  var proxy = null;
  var fracX = DEFAULT_FRAC_X;
  var fracY = DEFAULT_FRAC_Y;
  var composing = false;
  var warnedMissingHandler = false;

  function clamp(value, low, high) {
    return Math.min(Math.max(value, low), high);
  }

  function getInputHandler(silent) {
    var handler = window.webrtcInput;
    if (!handler ||
        typeof handler._compositionStart !== "function" ||
        typeof handler._compositionUpdate !== "function" ||
        typeof handler._compositionEnd !== "function") {
      if (!silent && !warnedMissingHandler) {
        warnedMissingHandler = true;
        console.warn(TAG, "Selkies composition handler is unavailable");
      }
      return null;
    }
    warnedMissingHandler = false;
    return handler;
  }

  function clearValue() {
    if (proxy && !composing && proxy.value) {
      proxy.value = "";
    }
  }

  function positionProxy() {
    if (!overlay || !proxy || !overlay.parentElement) return;

    var overlayRect = overlay.getBoundingClientRect();
    var parentRect = overlay.parentElement.getBoundingClientRect();
    if (overlayRect.width <= 0 || overlayRect.height <= 0) return;

    var anchorX = overlayRect.left + clamp(fracX, 0, 1) * overlayRect.width;
    var anchorY = overlayRect.top + clamp(fracY, 0, 1) * overlayRect.height;
    var minLeft = overlayRect.left - parentRect.left;
    var maxLeft = overlayRect.right - parentRect.left - PROXY_WIDTH_PX;
    var minTop = overlayRect.top - parentRect.top;
    var maxTop = overlayRect.bottom - parentRect.top - LINE_PX;

    var left = clamp(anchorX - parentRect.left, minLeft, Math.max(minLeft, maxLeft));
    var top = clamp(anchorY - parentRect.top - LINE_PX / 2,
                    minTop, Math.max(minTop, maxTop));

    proxy.style.left = Math.round(left) + "px";
    proxy.style.top = Math.round(top) + "px";
  }

  function focusProxy() {
    if (!proxy) return;
    clearValue();
    try {
      proxy.focus({ preventScroll: true });
    } catch (e) {
      proxy.focus();
    }
    try {
      var end = proxy.value.length;
      proxy.setSelectionRange(end, end);
    } catch (e) {
      // Selection APIs can be unavailable while the document is losing focus.
    }
  }

  function forwardComposition(method, event) {
    var handler = getInputHandler();
    if (!handler) return false;
    handler[method].call(handler, event);
    return true;
  }

  function onMouseDown(event) {
    if (!overlay || !proxy || event.target !== overlay || event.button !== 0) {
      return;
    }

    var rect = overlay.getBoundingClientRect();
    if (rect.width > 0 && rect.height > 0) {
      fracX = clamp((event.clientX - rect.left) / rect.width, 0, 1);
      fracY = clamp((event.clientY - rect.top) / rect.height, 0, 1);
    }
    positionProxy();

    // Do not prevent or stop this event: Selkies must still deliver the click
    // and drag to the remote desktop. Focus after the input's default action,
    // otherwise Chromium focuses #overlayInput again at the end of mousedown.
    setTimeout(focusProxy, 0);
  }

  function createProxy(parent) {
    var existing = document.getElementById(PROXY_ID);
    if (existing) {
      if (existing.tagName !== "TEXTAREA") {
        console.warn(TAG, "#" + PROXY_ID + " exists but is not a textarea");
        return null;
      }
      return existing;
    }

    var textarea = document.createElement("textarea");
    textarea.id = PROXY_ID;
    textarea.rows = 1;
    textarea.wrap = "off";
    textarea.tabIndex = -1;
    textarea.autocomplete = "off";
    textarea.setAttribute("autocorrect", "off");
    textarea.setAttribute("autocapitalize", "off");
    textarea.setAttribute("spellcheck", "false");
    textarea.setAttribute("aria-label", "Remote desktop IME input");

    var style = textarea.style;
    style.position = "absolute";
    style.left = "0px";
    style.top = "0px";
    style.width = PROXY_WIDTH_PX + "px";
    style.height = LINE_PX + "px";
    style.boxSizing = "border-box";
    style.padding = "0";
    style.border = "0";
    style.margin = "0";
    style.outline = "0";
    style.resize = "none";
    style.overflow = "hidden";
    style.whiteSpace = "nowrap";
    style.fontSize = "16px";
    style.lineHeight = LINE_PX + "px";
    style.opacity = "0";
    style.color = "transparent";
    style.background = "transparent";
    style.caretColor = "transparent";
    style.pointerEvents = "none";
    style.zIndex = "4";

    parent.appendChild(textarea);
    return textarea;
  }

  function attach(ov) {
    overlay = ov;

    // Remove the previous padding-based fix if this script is loaded into a
    // page that was patched in place during development.
    overlay.style.removeProperty("box-sizing");
    overlay.style.removeProperty("font-size");
    overlay.style.removeProperty("padding-left");
    overlay.style.removeProperty("padding-right");
    overlay.style.removeProperty("padding-top");
    overlay.style.removeProperty("padding-bottom");

    proxy = createProxy(overlay.parentElement);
    if (!proxy) return;

    window.addEventListener("mousedown", onMouseDown, true);

    proxy.addEventListener("compositionstart", function (event) {
      composing = forwardComposition("_compositionStart", event);
    });
    proxy.addEventListener("compositionupdate", function (event) {
      forwardComposition("_compositionUpdate", event);
    });
    proxy.addEventListener("compositionend", function (event) {
      forwardComposition("_compositionEnd", event);
      composing = false;
      setTimeout(clearValue, 0);
    });
    proxy.addEventListener("input", function (event) {
      if (!event.isComposing) setTimeout(clearValue, 0);
    });

    window.addEventListener("resize", function () {
      setTimeout(positionProxy, 100);
    });

    positionProxy();
    console.log(TAG, "using a click-local textarea for IME composition");
  }

  function boot() {
    var tries = 0;
    var timer = setInterval(function () {
      tries++;
      var ov = document.getElementById("overlayInput");
      if (ov && ov.parentElement && getInputHandler(true)) {
        clearInterval(timer);
        attach(ov);
      } else if (tries > 240) {
        clearInterval(timer);
        console.warn(TAG, "gave up waiting for Selkies input initialization");
      }
    }, 250);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
