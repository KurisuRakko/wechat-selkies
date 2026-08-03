#!/bin/sh
# Build-time patch: shorten pixelflux's damage-block duration.
#
# Usage: damage-block-duration.sh <frames> [site-packages-dir]
#
# pixelflux hashes each horizontal stripe once per frame (one XXH64 over the
# whole stripe at full width — there is no sub-block granularity) and re-encodes
# a stripe whenever its hash changes. When a stripe changes for
# damage_block_threshold consecutive frames it enters a "damage block": for the
# next damage_block_duration frames pixelflux stops hashing it and just encodes
# it every frame. At the end of the block it re-hashes and compares against the
# hash from the START of the block — if that differs, the block renews.
#
# So the knob trades hashing against wasted encodes, and upstream's 20 frames is
# tuned for video, not for a chat window:
#
#   - Motion that stops (typing, scrolling) leaves a tail of up to
#     2 x duration frames of full-stripe encodes after the screen is already
#     static — ~40 frames, two thirds of a second at 60 fps. At 4 that tail
#     drops to ~8 frames.
#   - The cost is re-hashing a busy stripe every 4 frames instead of every 20,
#     and hashing a stripe is far cheaper than JPEG/H.264-encoding it.
#   - It changes nothing for permanently animating content (a looping sticker):
#     the block renews forever either way. Only the post-motion tail improves.
#
# Selkies hardcodes these three values with no env var or UI exposure, which is
# why this has to be a patch. threshold (10) and paint_over_trigger_frames (15)
# are deliberately left alone: threshold is second-order here, and paint-over is
# what re-sends a settled stripe once at high quality to sharpen static text.
set -e

DURATION="${1:?usage: damage-block-duration.sh <frames> [site-packages-dir]}"
SITE="$2"
if [ -z "$SITE" ]; then
    # Glob the python version so this keeps working when the base image moves
    # off python3.12, rather than silently finding nothing.
    for d in /lsiopy/lib/python3.*/site-packages/selkies; do
        [ -d "$d" ] && SITE="$d" && break
    done
fi
[ -n "$SITE" ] || { echo "damage-block-duration: could not locate the selkies package" >&2; exit 1; }
TARGET="$SITE/selkies.py"

case "$DURATION" in
    ''|*[!0-9]*) echo "damage-block-duration: '$DURATION' is not a positive integer" >&2; exit 1 ;;
esac
[ "$DURATION" -ge 1 ] || { echo "damage-block-duration: must be >= 1" >&2; exit 1; }

[ -f "$TARGET" ] || { echo "damage-block-duration: $TARGET not found" >&2; exit 1; }

before=$(grep -c 'cs\.damage_block_duration = 20' "$TARGET" || true)
if [ "$before" -ne 1 ]; then
    echo "damage-block-duration: expected exactly 1 site of 'cs.damage_block_duration = 20' in $TARGET, found $before" >&2
    exit 1
fi

sed -i "s/cs\.damage_block_duration = 20/cs.damage_block_duration = $DURATION/" "$TARGET"

after=$(grep -c "cs\.damage_block_duration = $DURATION" "$TARGET" || true)
if [ "$after" -ne 1 ]; then
    echo "damage-block-duration: FAILED to rewrite $TARGET" >&2
    exit 1
fi

# The bundled .pyc would otherwise be consulted first. Python invalidates it on
# the .py's mtime, which sed just changed, but drop it so nothing can shadow the
# patched source.
rm -rf "$SITE/__pycache__"

for PY in /lsiopy/bin/python3 python3 python; do
    command -v "$PY" >/dev/null 2>&1 || continue
    "$PY" -c "import ast,sys; ast.parse(open(sys.argv[1], encoding='utf-8').read())" "$TARGET" || {
        echo "damage-block-duration: $TARGET no longer parses after patching" >&2
        exit 1
    }
    break
done

echo "damage-block-duration: $TARGET -> damage_block_duration = $DURATION (threshold and paint_over left at upstream values)"
