#!/bin/sh
# Build-time patch: shorten the frame-backpressure poll interval.
#
# Usage: backpressure-check-interval.sh <seconds> [site-packages-dir]
#
# _run_frame_backpressure_logic() re-evaluates every display's desync once per
# BACKPRESSURE_CHECK_INTERVAL_S (selkies.py module constant, upstream default
# 0.5s): it compares the server's last-sent frame id against the client's last
# acknowledged one and flips backpressure_enabled on/off accordingly. Because
# the primary-display broadcast path only sends a frame when
# backpressure_enabled is true (patches/input-and-backpressure-fixes.py fix 3),
# this interval is the granularity at which a slow link's "stop sending" and a
# recovered link's "resume sending" actually take effect: at 0.5s, a transient
# stall can freeze the picture for up to half a second longer than the network
# itself already recovered, and lifting takes just as long to notice.
#
# Trade-off, stated plainly rather than hidden: a faster poll reacts sooner in
# both directions, but if the desync signal hovers right at the
# allowed-desync threshold, checking it more often can also flip
# backpressure_enabled more often. This is a real, unresolved trade-off (see
# the plan's risk section) -- the value below is a conservative first step
# (2.5x faster, not more), and is kept as a build ARG specifically so it can
# be dialed back to the upstream 0.5 default with no code change if it ever
# reads as more stutter instead of less.
set -e

INTERVAL="${1:?usage: backpressure-check-interval.sh <seconds> [site-packages-dir]}"
SITE="$2"
if [ -z "$SITE" ]; then
    # Glob the python version so this keeps working when the base image moves
    # off python3.12, rather than silently finding nothing.
    for d in /lsiopy/lib/python3.*/site-packages/selkies; do
        [ -d "$d" ] && SITE="$d" && break
    done
fi
[ -n "$SITE" ] || { echo "backpressure-check-interval: could not locate the selkies package" >&2; exit 1; }
TARGET="$SITE/selkies.py"

PY=""
for candidate in /lsiopy/bin/python3 python3 python; do
    command -v "$candidate" >/dev/null 2>&1 || continue
    PY="$candidate"
    break
done
[ -n "$PY" ] || { echo "backpressure-check-interval: no python3 interpreter found to validate '$INTERVAL'" >&2; exit 1; }

"$PY" -c "
import sys
try:
    value = float(sys.argv[1])
except ValueError:
    sys.exit('not a number')
if not (0 < value <= 5):
    sys.exit('out of the sane 0-5s range')
" "$INTERVAL" || { echo "backpressure-check-interval: '$INTERVAL' must be a number in (0, 5]" >&2; exit 1; }

[ -f "$TARGET" ] || { echo "backpressure-check-interval: $TARGET not found" >&2; exit 1; }

before=$(grep -c 'BACKPRESSURE_CHECK_INTERVAL_S = 0.5' "$TARGET" || true)
if [ "$before" -ne 1 ]; then
    echo "backpressure-check-interval: expected exactly 1 site of 'BACKPRESSURE_CHECK_INTERVAL_S = 0.5' in $TARGET, found $before" >&2
    exit 1
fi

sed -i "s/BACKPRESSURE_CHECK_INTERVAL_S = 0.5/BACKPRESSURE_CHECK_INTERVAL_S = $INTERVAL/" "$TARGET"

after=$(grep -c "BACKPRESSURE_CHECK_INTERVAL_S = $INTERVAL" "$TARGET" || true)
if [ "$after" -ne 1 ]; then
    echo "backpressure-check-interval: FAILED to rewrite $TARGET" >&2
    exit 1
fi

# The bundled .pyc would otherwise be consulted first. Python invalidates it on
# the .py's mtime, which sed just changed, but drop it so nothing can shadow
# the patched source.
rm -rf "$SITE/__pycache__"

"$PY" -c "import ast,sys; ast.parse(open(sys.argv[1], encoding='utf-8').read())" "$TARGET" || {
    echo "backpressure-check-interval: $TARGET no longer parses after patching" >&2
    exit 1
}

echo "backpressure-check-interval: $TARGET -> BACKPRESSURE_CHECK_INTERVAL_S = $INTERVAL"
