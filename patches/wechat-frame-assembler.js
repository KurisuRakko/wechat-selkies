/*
 * Fixes horizontal tearing in the striped (x264enc-striped) video path.
 *
 * pixelflux cuts each frame into horizontal Y-stripes and streams them
 * independently; the bundle runs one VideoDecoder per stripe Y-position and
 * pushes every decoded output into one global paint queue, which the render
 * loop flushes unconditionally on every rAF. Nothing ties a decoded stripe
 * back to the server-side frame it belongs to, so frame N's top half and
 * frame N+1's bottom half can land in the same paint pass — a visible seam.
 *
 * The bundle already reads a vncFrameID off the wire, but only binds it to a
 * decoder's *creation* time (WeChat-Selkies-side patch: see
 * patches/patch-frame-assembly.py), so it goes stale the moment a decoder is
 * reused across frames. This module tracks frame ids per stripe, keyed by
 * actual submission order, and only lets the render loop paint stripes that
 * belong to the same frame — or, if that data ever gets out of sync, releases
 * whatever is queued after a short timeout so the picture never freezes.
 *
 * Correctness rests on one property of the format: WebCodecs guarantees a
 * VideoDecoder's outputs arrive in the same order chunks were submitted to
 * it (x264enc-striped is encoded zerolatency, no B-frames, so there is no
 * reordering upstream either). Each Y-position has its own decoder, so a FIFO
 * of submitted frame ids per Y, shifted on every decoded output, recovers the
 * true frame id no matter how the global queue interleaves stripes from
 * different decoders.
 */
(function () {
  "use strict";

  if (window.wechatFrameAssembler) return;

  // Worst case a static screen sits mid-frame for 80ms before painting
  // anyway — long enough to let slow stripes catch up, short enough that a
  // stalled/lost stripe never reads as a frozen picture.
  var HOLD_MS = 80;
  var MASK = 0xffff;
  // Frame ids are wire-format uint16 (getUint16), so comparisons must be
  // wrap-aware: 65535 is one frame older than 0, not 65535 frames newer.
  var ORPHAN_AGE = 256;

  // Per Y-position FIFO of frame ids submitted to that stripe's decoder, in
  // the exact order decode() was called — this is what lets a decoded output
  // recover which frame it belonged to.
  var perY = Object.create(null);
  // Outstanding stripe count per frame id, across all Y-positions.
  var count = new Map();
  // Most recently *submitted* frame id (wrap-aware "latest").
  var cursor = null;
  var lastPainted = false;

  function isNewer(a, b) {
    var diff = (a - b) & MASK;
    return diff !== 0 && diff < 0x8000;
  }

  // Wrap-aware distance behind cursor: 0 for the current frame, increasing
  // for older ones.
  function age(id) {
    return (cursor - id) & MASK;
  }

  function decCount(id) {
    var c = count.get(id);
    if (c === undefined) return;
    if (c <= 1) count.delete(id);
    else count.set(id, c - 1);
  }

  // Called right before a stripe chunk is handed to its decoder (both the
  // immediate decode() path and the pendingChunks queue, which preserves
  // arrival order so calling this at enqueue time keeps the FIFO correct).
  function submitted(y, id) {
    var arr = perY[y];
    if (!arr) {
      arr = [];
      perY[y] = arr;
    }
    arr.push(id);
    count.set(id, (count.get(id) || 0) + 1);
    if (cursor === null || isNewer(id, cursor)) cursor = id;
  }

  // Called when decode() throws synchronously, to undo the matching submitted().
  function submitFailed(y) {
    var arr = perY[y];
    if (!arr || !arr.length) return;
    decCount(arr.pop());
  }

  // Called from the decoder's output callback. staleId is the id the bundle
  // bound at decoder-creation time (stale the moment the decoder is reused);
  // it is only used as a fallback if the FIFO is empty, which self-heals a
  // desync instead of ever throwing.
  function entry(y, staleId, frame) {
    var arr = perY[y];
    var realId = arr && arr.length ? arr.shift() : staleId;
    return { yPos: y, frame: frame, vncFrameID: realId, __t: performance.now() };
  }

  // Called when a stripe's decoder is torn down/recreated (close/config
  // failure): whatever the FIFO still thinks is in flight for this Y never
  // arrives, so roll those counts back rather than leaking them.
  function resetStripe(y) {
    var arr = perY[y];
    if (arr) {
      for (var i = 0; i < arr.length; i++) decCount(arr[i]);
    }
    perY[y] = [];
  }

  // Consumes the raw decoded-output queue and returns what should stay
  // queued. Paints (and closes) every stripe of a frame id only once that
  // frame is known complete, or once it has waited past HOLD_MS.
  function drain(queue, ctx, canvas) {
    var now = performance.now();
    var groups = new Map();
    for (var i = 0; i < queue.length; i++) {
      var e = queue[i];
      var list = groups.get(e.vncFrameID);
      if (!list) {
        list = [];
        groups.set(e.vncFrameID, list);
      }
      list.push(e);
    }

    // Sweep counts for frame ids that fell far enough behind cursor that
    // they will never be completed — a lost stripe with no matching
    // resetStripe/submitFailed would otherwise leak here forever.
    if (cursor !== null) {
      count.forEach(function (_v, id) {
        if (age(id) > ORPHAN_AGE) count.delete(id);
      });
    }

    var ids = Array.from(groups.keys());
    // Oldest first, wrap-aware, so frames paint in the order they occurred.
    ids.sort(function (a, b) { return age(b) - age(a); });

    var painted = false;
    var handled = new Set();

    for (var gi = 0; gi < ids.length; gi++) {
      var id = ids[gi];
      var entries = groups.get(id);
      var needed = count.has(id) ? count.get(id) : entries.length;
      var earliest = Infinity;
      for (var j = 0; j < entries.length; j++) {
        if (entries[j].__t < earliest) earliest = entries[j].__t;
      }
      var complete = isNewer(cursor, id) && entries.length >= needed;
      var timedOut = now - earliest > HOLD_MS;
      if (complete || timedOut) {
        for (var k = 0; k < entries.length; k++) {
          var e2 = entries[k];
          if (canvas.width > 0 && canvas.height > 0) ctx.drawImage(e2.frame, 0, e2.yPos);
          e2.frame.close();
        }
        count.delete(id);
        painted = true;
        handled.add(id);
      }
    }

    var remaining = [];
    for (var qi = 0; qi < queue.length; qi++) {
      if (!handled.has(queue[qi].vncFrameID)) remaining.push(queue[qi]);
    }

    lastPainted = painted;
    return remaining;
  }

  window.wechatFrameAssembler = Object.freeze({
    submitted: submitted,
    submitFailed: submitFailed,
    entry: entry,
    resetStripe: resetStripe,
    drain: drain,
    get painted() { return lastPainted; }
  });
})();
