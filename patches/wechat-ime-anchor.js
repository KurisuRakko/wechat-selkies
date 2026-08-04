/*
 * Put the host IME's candidate window where the user is actually typing.
 *
 * All keyboard input for the stream lands in #overlayInput, an invisible
 * <input> stretched over the whole video. The browser anchors the IME
 * composition/candidate popup to that input's caret, and the caret of an
 * empty full-screen input sits at its centre-left — so the candidate list
 * popped up in the middle of nowhere instead of beside WeChat's text box.
 * (Same class of bug as noVNC's IME popup appearing at the screen corner
 * where its hidden input lives.)
 *
 * Fix: every click on the stream moves the overlay's caret to the click
 * point. The element itself cannot move (it is the mouse-event target for
 * the whole video), but with box-sizing:border-box its *padding* can herd
 * the caret anywhere without changing the element's geometry, which keeps
 * the bundle's mouse math untouched:
 *
 *   horizontal: caret x = padding-left            (value is kept empty)
 *   vertical:   padding-top/-bottom squeeze the content box to one line
 *               height centred on the click y, pinning the caret there
 *
 * The second half of the fix is value hygiene. Nothing ever cleared
 * #overlayInput.value, so every committed phrase accumulated in the hidden
 * input and the caret (= the IME popup) drifted further right with every
 * message typed. The committed text is worthless — the stream types via
 * key events and "co,end" messages, never from this value — so drop it as
 * soon as each composition is over.
 *
 * Runs as a plain script beside the bundle (same pattern as
 * src/wechat-dragdrop.js); nothing minified is touched.
 */
(function () {
  "use strict";

  var TAG = "[wechat-ime-anchor]";

  // Read-only viewers get no keyboard input; match wechat-dragdrop's guard.
  if (String(location.hash).indexOf("shared") !== -1 ||
      String(location.hash).indexOf("player") !== -1) {
    return;
  }

  // Keep a page-level margin so the caret (and the popup anchored to it)
  // never gets pinned into a border where the popup would cover the text.
  var EDGE_PX = 24;
  // Height the content box is squeezed to; must stay >= the caret's line
  // height (14px font => ~17px) or the browser would refuse the padding.
  var LINE_PX = 20;

  // Until the first click, anchor roughly where a maximized WeChat puts its
  // message box, so even the very first composition pops up near it.
  var fracX = 0.4;
  var fracY = 0.85;

  var overlay = null;
  var composing = false;

  function applyAnchor() {
    if (!overlay) return;
    var rect = overlay.getBoundingClientRect();
    if (rect.width < EDGE_PX * 2 || rect.height < EDGE_PX * 2) return;

    var x = Math.min(Math.max(fracX * rect.width, EDGE_PX), rect.width - EDGE_PX);
    var y = Math.min(Math.max(fracY * rect.height, EDGE_PX), rect.height - EDGE_PX);

    // Squeeze the content box down to a single line height centred on the
    // click y. With the content box exactly one line tall the caret sits at
    // (padTop + LINE_PX/2) no matter how the browser vertically aligns text
    // inside an input, so this does not depend on Chrome's centring rule.
    var h = rect.height;
    var padTop = Math.max(0, Math.min(y - LINE_PX / 2, h - LINE_PX));
    var padBottom = Math.max(0, h - padTop - LINE_PX);

    // border-box: padding moves the caret, never the element's box, so the
    // bundle's coordinate math and the resize handler stay correct.
    overlay.style.boxSizing = "border-box";
    // A predictable caret rect for the popup to anchor to; the element is
    // opacity:0 so none of this renders.
    overlay.style.fontSize = "14px";
    overlay.style.paddingLeft = Math.round(x) + "px";
    overlay.style.paddingRight = "0px";
    overlay.style.paddingTop = Math.round(padTop) + "px";
    overlay.style.paddingBottom = Math.round(padBottom) + "px";
  }

  function clearValue() {
    // Never while the IME is mid-composition: yanking the value out from
    // under it would cancel or scramble the pre-edit.
    if (overlay && !composing && overlay.value) {
      overlay.value = "";
    }
  }

  function onMouseDown(e) {
    if (!overlay || e.target !== overlay) return;
    var rect = overlay.getBoundingClientRect();
    if (rect.width > 0 && rect.height > 0) {
      fracX = (e.clientX - rect.left) / rect.width;
      fracY = (e.clientY - rect.top) / rect.height;
    }
    clearValue();
    applyAnchor();
  }

  function attach(ov) {
    overlay = ov;

    // Capture phase and window-level: the bundle stops propagation of some
    // events on the overlay, but composition/mousedown reach us regardless.
    window.addEventListener("mousedown", onMouseDown, true);

    window.addEventListener("compositionstart", function (e) {
      if (e.target === overlay) composing = true;
    }, true);

    window.addEventListener("compositionend", function (e) {
      if (e.target !== overlay) return;
      composing = false;
      // The browser inserts the committed text into the value after this
      // event; clear once that has happened (guarded in clearValue if a new
      // composition started in the meantime).
      setTimeout(clearValue, 0);
    }, true);

    // Non-composition insertions (e.g. emoji picker, paste into the page)
    // would otherwise accumulate exactly like commits did.
    overlay.addEventListener("input", function (e) {
      if (!e.isComposing) setTimeout(clearValue, 0);
    });

    // The bundle rewrites the overlay's size on every resize; padding
    // survives, but its clamping needs to be redone for the new box.
    window.addEventListener("resize", function () {
      setTimeout(applyAnchor, 100);
    });

    applyAnchor();
    console.log(TAG, "anchoring IME popup to clicks on #overlayInput");
  }

  function boot() {
    var tries = 0;
    var t = setInterval(function () {
      tries++;
      var ov = document.getElementById("overlayInput");
      if (ov) {
        clearInterval(t);
        attach(ov);
      } else if (tries > 120) {
        clearInterval(t);
        console.warn(TAG, "gave up waiting for #overlayInput");
      }
    }, 500);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
