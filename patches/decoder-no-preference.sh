#!/bin/sh
# Build-time patch: stop forcing SOFTWARE video decoding in the browser.
#
# Upstream configures every WebCodecs VideoDecoder with
#   hardwareAcceleration: "prefer-software"
# which tells the browser not to use the client machine's hardware H.264
# decoder. That matters beyond decode cost: in websockets mode one WebSocket
# carries video, audio, clipboard AND input, and frames are decoded on the
# browser's main thread — the same thread that delivers mousemove. Starving it
# shows up as clicks landing at a stale pointer position, because mousedown
# reuses this.x/this.y from the last mousemove it managed to process.
#
# "no-preference" is the WebCodecs default: the browser picks, and falls back to
# software cleanly when a hardware decoder is unavailable. Deliberately NOT
# "prefer-hardware" — hardware decoders add pipeline latency, and in the
# x264enc-striped path each frame is a small stripe, where per-frame hardware
# overhead can outweigh the saving. Upstream's choice is defensible; this only
# stops overriding the browser's own judgement.
#
# Revert by rebuilding without this patch.
set -e

ROOT="${1:-/usr/share/selkies}"

files=$(grep -rl 'hardwareAcceleration:"prefer-software"' "$ROOT" 2>/dev/null || true)
if [ -z "$files" ]; then
    echo "decoder-no-preference: no file under $ROOT sets hardwareAcceleration:\"prefer-software\"" >&2
    exit 1
fi

patched=0
for f in $files; do
    sed -i 's/hardwareAcceleration:"prefer-software"/hardwareAcceleration:"no-preference"/g' "$f"

    # Every occurrence must be gone. A partial rewrite would leave one decoder
    # (main vs secondary display) on software and make behaviour inconsistent
    # and hard to reason about, so fail the build instead.
    left=$(grep -o 'hardwareAcceleration:"prefer-software"' "$f" | wc -l)
    now=$(grep -o 'hardwareAcceleration:"no-preference"' "$f" | wc -l)
    if [ "$left" -ne 0 ] || [ "$now" -lt 1 ]; then
        echo "decoder-no-preference: FAILED on $f (prefer-software left=$left, no-preference=$now)" >&2
        exit 1
    fi
    echo "decoder-no-preference: patched $f ($now decoder config(s))"
    patched=$((patched + 1))
done

echo "decoder-no-preference: $patched file(s) patched"
