#!/bin/sh
# Build-time patch: load patches/wechat-connection-status.js from the
# dashboard's HTML.
#
# Same pattern as inject-ime-anchor-script.sh: the dashboard already loads plain
# (non-module) scripts next to the bundle, so the status pill is added the same
# way instead of editing the minified bundle. It only reads window.network_stats,
# which the bundle already publishes.
set -e

ROOT="${1:-/usr/share/selkies}"
SCRIPT_REL="src/wechat-connection-status.js"
TAG="<script src=\"$SCRIPT_REL\"></script>"

found=0
for html in "$ROOT"/*/index.html; do
    [ -f "$html" ] || continue
    dir=$(dirname "$html")
    bundle="$dir/src/selkies-core.js"

    # Only dashboards that actually carry the streaming client.
    [ -f "$dir/$SCRIPT_REL" ] || continue

    if [ ! -f "$bundle" ]; then
        echo "inject-connection-status-script: missing Selkies bundle beside $html" >&2
        exit 1
    fi

    # window.fps is not displayed (a static screen legitimately encodes 0 fps)
    # but its absence would mean the stats plumbing has been reshaped, which is
    # exactly when this script's assumptions need re-checking.
    for api in window.network_stats window.fps; do
        if ! grep -Fq "$api" "$bundle"; then
            echo "inject-connection-status-script: required API $api missing from $bundle" >&2
            exit 1
        fi
    done

    tag_count=$(grep -Fo "$TAG" "$html" | wc -l | tr -d ' ')
    if [ "$tag_count" -gt 1 ]; then
        echo "inject-connection-status-script: duplicate script tags in $html" >&2
        exit 1
    fi

    if [ "$tag_count" -eq 1 ]; then
        echo "inject-connection-status-script: already present in $html"
        found=$((found + 1))
        continue
    fi

    if ! grep -q "</body>" "$html"; then
        echo "inject-connection-status-script: no </body> in $html" >&2
        exit 1
    fi

    sed -i "s|</body>|  $TAG\n</body>|" "$html"

    tag_count=$(grep -Fo "$TAG" "$html" | wc -l | tr -d ' ')
    [ "$tag_count" -eq 1 ] || {
        echo "inject-connection-status-script: FAILED to inject into $html" >&2
        exit 1
    }
    echo "inject-connection-status-script: injected into $html"
    found=$((found + 1))
done

if [ "$found" -eq 0 ]; then
    echo "inject-connection-status-script: found no dashboard with both index.html and $SCRIPT_REL" >&2
    exit 1
fi

echo "inject-connection-status-script: $found dashboard(s) wired up"
