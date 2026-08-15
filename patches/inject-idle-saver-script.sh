#!/bin/sh
# Build-time patch: load patches/wechat-idle-saver.js from the dashboard's HTML.
#
# Same pattern as inject-quality-presets-script.sh. The idle saver drives the
# bundle through the very same {type:"settings"} postMessage the sidebar posts
# and the same per-URL localStorage keys the bundle reads at startup, so the
# required APIs below are what must still exist for it to work at all. encoder
# and rate_control_mode are deliberately NOT checked: this script never writes
# those two keys (the locked-settings script owns them).
set -e

ROOT="${1:-/usr/share/selkies}"
SCRIPT_REL="src/wechat-idle-saver.js"
TAG="<script src=\"$SCRIPT_REL\"></script>"

found=0
for html in "$ROOT"/*/index.html; do
    [ -f "$html" ] || continue
    dir=$(dirname "$html")
    bundle="$dir/src/selkies-core.js"

    # Only dashboards that actually carry the streaming client.
    [ -f "$dir/$SCRIPT_REL" ] || continue

    if [ ! -f "$bundle" ]; then
        echo "inject-idle-saver-script: missing Selkies bundle beside $html" >&2
        exit 1
    fi

    # The settings channel and the three keys the idle saver writes. If any of
    # these disappear the saver would silently do nothing, so fail the build
    # instead.
    for api in \
        'case"settings"' \
        '"framerate"' \
        '"video_bitrate"' \
        '"h264_paintover_crf"' \
        'a-zA-Z0-9.-_'; do
        if ! grep -Fq "$api" "$bundle"; then
            echo "inject-idle-saver-script: required API $api missing from $bundle" >&2
            exit 1
        fi
    done

    tag_count=$(grep -Fo "$TAG" "$html" | wc -l | tr -d ' ')
    if [ "$tag_count" -gt 1 ]; then
        echo "inject-idle-saver-script: duplicate script tags in $html" >&2
        exit 1
    fi

    if [ "$tag_count" -eq 1 ]; then
        echo "inject-idle-saver-script: already present in $html"
        found=$((found + 1))
        continue
    fi

    if ! grep -q "</body>" "$html"; then
        echo "inject-idle-saver-script: no </body> in $html" >&2
        exit 1
    fi

    sed -i "s|</body>|  $TAG\n</body>|" "$html"

    tag_count=$(grep -Fo "$TAG" "$html" | wc -l | tr -d ' ')
    [ "$tag_count" -eq 1 ] || {
        echo "inject-idle-saver-script: FAILED to inject into $html" >&2
        exit 1
    }
    echo "inject-idle-saver-script: injected into $html"
    found=$((found + 1))
done

if [ "$found" -eq 0 ]; then
    echo "inject-idle-saver-script: found no dashboard with both index.html and $SCRIPT_REL" >&2
    exit 1
fi

echo "inject-idle-saver-script: $found dashboard(s) wired up"
