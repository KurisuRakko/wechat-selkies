#!/usr/bin/env node
"use strict";

// Unit tests for patches/wechat-frame-assembler.js, in the same node:vm style
// as test-wechat-quality-presets.js.
//
// This is the piece that actually fixes the tearing: it must attribute every
// decoded stripe to the right frame id (even when two decoders interleave
// their output), withhold an incomplete frame from painting, and never get
// stuck holding stripes forever when the FIFO bookkeeping goes out of sync.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function sleepSync(ms) {
  const sab = new Int32Array(new SharedArrayBuffer(4));
  Atomics.wait(sab, 0, 0, ms);
}

function makeFrame(label) {
  return {
    label,
    closed: 0,
    close() { this.closed += 1; },
  };
}

function makeCanvasCtx() {
  const draws = [];
  return {
    canvas: { width: 100, height: 100 },
    ctx: { drawImage(frame, x, y) { draws.push({ frame: frame.label, x, y }); } },
    draws,
  };
}

global.window = {};
global.performance = performance;

const scriptPath = process.argv[2] ||
  path.join(__dirname, "wechat-frame-assembler.js");
const source = fs.readFileSync(scriptPath, "utf8");

function load() {
  delete global.window.wechatFrameAssembler;
  vm.runInThisContext(source, { filename: scriptPath });
  return global.window.wechatFrameAssembler;
}

/* 0. installs itself once, exposes the expected surface -------------------- */
{
  const wfa = load();
  assert.ok(wfa, "module installs onto window.wechatFrameAssembler");
  for (const key of ["submitted", "submitFailed", "entry", "resetStripe", "drain"]) {
    assert.equal(typeof wfa[key], "function", `${key} is a function`);
  }
  assert.equal(wfa.painted, false, "painted starts false");

  const before = wfa;
  window.wechatFrameAssembler = "sentinel";
  vm.runInThisContext(source, { filename: scriptPath });
  assert.equal(window.wechatFrameAssembler, "sentinel", "does not reinstall itself");
}

/* 1. interleaved stripes attribute correctly, complete groups paint together */
{
  const wfa = load();
  const { canvas, ctx, draws } = makeCanvasCtx();

  // Frame 100 has two stripes (Y=0, Y=200); a slower decoder on Y=200 means
  // frame 101's Y=0 stripe can decode and arrive before frame 100's Y=200.
  wfa.submitted(0, 100);
  wfa.submitted(200, 100);
  wfa.submitted(0, 101);

  let queue = [];
  queue.push(wfa.entry(0, 999, makeFrame("f100y0")));   // frame 100, Y=0 arrives first
  queue.push(wfa.entry(0, 999, makeFrame("f101y0")));   // frame 101, Y=0 arrives next (same decoder)

  queue = wfa.drain(queue, ctx, canvas);
  // Frame 100 is incomplete (Y=200 stripe not decoded yet) so nothing paints,
  // even though frame 101 already looks "newer".
  assert.equal(draws.length, 0, "incomplete frame withheld from painting");
  assert.equal(queue.length, 2, "both stripes remain queued");

  // The slow Y=200 decoder finally produces frame 100's second stripe.
  queue.push(wfa.entry(200, 999, makeFrame("f100y200")));
  queue = wfa.drain(queue, ctx, canvas);

  assert.equal(wfa.painted, true);
  assert.deepEqual(
    draws.map((d) => d.frame).sort(),
    ["f100y0", "f100y200"],
    "only frame 100's stripes painted, in one pass"
  );
  assert.equal(queue.length, 1, "frame 101's lone stripe stays held back");
  assert.equal(queue[0].frame.label, "f101y0");
}

/* 2. the newest complete group releases once cursor advances past it ------- */
{
  const wfa = load();
  const { canvas, ctx, draws } = makeCanvasCtx();

  wfa.submitted(0, 5);
  let queue = [wfa.entry(0, 999, makeFrame("f5"))];
  queue = wfa.drain(queue, ctx, canvas);
  // Frame 5 is the current cursor (nothing newer submitted yet) — held back
  // even though its only stripe already arrived.
  assert.equal(draws.length, 0, "current frame held until a newer one starts");
  assert.equal(queue.length, 1);

  // Frame 6 begins — cursor advances past 5.
  wfa.submitted(0, 6);
  queue = wfa.drain(queue, ctx, canvas);
  assert.equal(draws.length, 1, "frame 5 releases once cursor moves past it");
  assert.equal(draws[0].frame, "f5");
  assert.equal(queue.length, 0, "frame 6's own stripe not yet decoded, so nothing else queued");
}

/* 3. a static picture's last frame paints after HOLD_MS, not forever ------- */
{
  const wfa = load();
  const { canvas, ctx, draws } = makeCanvasCtx();

  wfa.submitted(0, 40);
  wfa.submitted(200, 40);
  let queue = [wfa.entry(0, 999, makeFrame("last-top"))];
  // Y=200's stripe never arrives (stream went idle) — nothing newer submitted
  // either, so the "cursor passed it" condition never fires.
  queue = wfa.drain(queue, ctx, canvas);
  assert.equal(draws.length, 0, "not timed out yet");

  sleepSync(90);
  queue = wfa.drain(queue, ctx, canvas);
  assert.equal(draws.length, 1, "HOLD_MS timeout force-releases the incomplete frame");
  assert.equal(draws[0].frame, "last-top");
  assert.equal(queue.length, 0);
}

/* 4. submitFailed rolls back the count a synchronous decode() throw added -- */
{
  const wfa = load();
  const { canvas, ctx, draws } = makeCanvasCtx();

  wfa.submitted(0, 10);
  wfa.submitted(200, 10);
  wfa.submitFailed(200); // Y=200's decode() threw synchronously; undo it.

  let queue = [wfa.entry(0, 999, makeFrame("y0-only"))];
  wfa.submitted(0, 11); // something newer starts
  queue = wfa.drain(queue, ctx, canvas);

  // count[10] should now be 1 (only the Y=0 stripe), so one arrived stripe
  // is "complete" and paints without waiting for HOLD_MS.
  assert.equal(draws.length, 1, "rolled-back count lets the frame complete with one stripe");
  assert.equal(queue.length, 0);
}

/* 5. resetStripe rolls back everything still queued for a torn-down decoder */
{
  const wfa = load();
  const { canvas, ctx, draws } = makeCanvasCtx();

  wfa.submitted(200, 20);
  wfa.submitted(200, 21); // two chunks in flight on Y=200's decoder
  wfa.resetStripe(200);   // decoder recreated (config change) before either decoded

  wfa.submitted(0, 20);
  wfa.submitted(0, 21);
  let queue = [wfa.entry(0, 999, makeFrame("f20y0"))];
  wfa.submitted(0, 22);
  queue = wfa.drain(queue, ctx, canvas);

  // Frame 20's Y=200 stripe was rolled back out of count, so Y=0 alone is
  // "complete" and paints without waiting on a stripe that will never come.
  assert.equal(draws.length, 1, "reset stripe's rollback lets the frame complete");
  assert.equal(draws[0].frame, "f20y0");
}

/* 6. FIFO desync falls back to the stale id instead of throwing ------------ */
{
  const wfa = load();
  // entry() called with nothing ever submitted() for this Y: falls back to
  // the caller-supplied stale id rather than throwing on an empty shift().
  const e = wfa.entry(0, 12345, makeFrame("orphan"));
  assert.equal(e.vncFrameID, 12345);
  assert.equal(e.yPos, 0);
}

/* 7. uint16 wraparound: 65534 -> 65535 -> 0 -> 1 sorts and groups correctly */
{
  const wfa = load();
  const { canvas, ctx, draws } = makeCanvasCtx();

  wfa.submitted(0, 65534);
  wfa.submitted(0, 65535);
  wfa.submitted(0, 0);
  wfa.submitted(0, 1);

  let queue = [
    wfa.entry(0, 999, makeFrame("w1")),
    wfa.entry(0, 999, makeFrame("w2")),
    wfa.entry(0, 999, makeFrame("w3")),
  ];
  // Cursor is now 1 (the newest submission). 65534, 65535 and 0 are all
  // strictly older than 1 under wrap-aware comparison, so all three of their
  // single-stripe groups are complete and release in oldest-first order.
  queue = wfa.drain(queue, ctx, canvas);

  assert.deepEqual(
    draws.map((d) => d.frame),
    ["w1", "w2", "w3"],
    "wrapped ids 65534, 65535, 0 paint oldest-first despite the numeric wrap"
  );
  assert.equal(queue.length, 0);
}

/* 8. every painted frame closes exactly once; held-back frames are untouched */
{
  const wfa = load();
  const { canvas, ctx } = makeCanvasCtx();

  wfa.submitted(0, 200);
  const painted = makeFrame("painted");
  let queue = [wfa.entry(0, 999, painted)]; // frame 200

  wfa.submitted(0, 201); // cursor advances to 201, making frame 200 releasable
  const held = makeFrame("held");
  queue.push(wfa.entry(0, 999, held)); // frame 201 — this is now the current cursor

  queue = wfa.drain(queue, ctx, canvas);

  assert.equal(painted.closed, 1, "painted frame closed exactly once");
  assert.equal(held.closed, 0, "held-back frame is never closed");
  assert.equal(queue.length, 1);
  assert.equal(queue[0].frame, held);
}

console.log("wechat-frame-assembler unit tests passed");
