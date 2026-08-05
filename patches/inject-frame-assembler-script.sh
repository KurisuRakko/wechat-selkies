#!/bin/sh
# Build-time patch: load patches/wechat-frame-assembler.js from the dashboard's
# HTML, before patch-frame-assembly.py wires the minified bundle to call it.
#
# Same pattern as inject-quality-presets-script.sh. This must run as a plain
# classic <script>, before the deferred module bundle boots, so
# window.wechatFrameAssembler exists by the time the bundle's decoder output
# callbacks and render loop start firing.
set -e

ROOT="${1:-/usr/share/selkies}"
SCRIPT_REL="src/wechat-frame-assembler.js"
TAG="<script src=\"$SCRIPT_REL\"></script>"

found=0
for html in "$ROOT"/*/index.html; do
    [ -f "$html" ] || continue
    dir=$(dirname "$html")
    bundle="$dir/src/selkies-core.js"

    # Only dashboards that actually carry the streaming client.
    [ -f "$dir/$SCRIPT_REL" ] || continue

    if [ ! -f "$bundle" ]; then
        echo "inject-frame-assembler-script: missing Selkies bundle beside $html" >&2
        exit 1
    fi

    # The APIs patch-frame-assembly.py hooks into. If any of these disappear
    # the assembler would have nothing to attach to, so fail the build instead
    # of shipping a script that never gets called.
    for api in \
        'vncFrameID:' \
        'stripe_decode_Y=' \
        '.pendingChunks.push(' \
        'Error decoding pending chunk for stripe Y=' \
        'Error closing old VNC stripe decoder:' \
        'Error configuring VNC stripe decoder'; do
        if ! grep -Fq "$api" "$bundle"; then
            echo "inject-frame-assembler-script: required API $api missing from $bundle" >&2
            exit 1
        fi
    done

    tag_count=$(grep -Fo "$TAG" "$html" | wc -l | tr -d ' ')
    if [ "$tag_count" -gt 1 ]; then
        echo "inject-frame-assembler-script: duplicate script tags in $html" >&2
        exit 1
    fi

    if [ "$tag_count" -eq 1 ]; then
        echo "inject-frame-assembler-script: already present in $html"
        found=$((found + 1))
        continue
    fi

    if ! grep -q "</body>" "$html"; then
        echo "inject-frame-assembler-script: no </body> in $html" >&2
        exit 1
    fi

    sed -i "s|</body>|  $TAG\n</body>|" "$html"

    tag_count=$(grep -Fo "$TAG" "$html" | wc -l | tr -d ' ')
    [ "$tag_count" -eq 1 ] || {
        echo "inject-frame-assembler-script: FAILED to inject into $html" >&2
        exit 1
    }
    echo "inject-frame-assembler-script: injected into $html"
    found=$((found + 1))
done

if [ "$found" -eq 0 ]; then
    echo "inject-frame-assembler-script: found no dashboard with both index.html and $SCRIPT_REL" >&2
    exit 1
fi

echo "inject-frame-assembler-script: $found dashboard(s) wired up"
