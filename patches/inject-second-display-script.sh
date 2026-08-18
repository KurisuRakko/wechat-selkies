#!/bin/sh
# Build-time patch: load patches/wechat-second-display.js from the
# dashboard's HTML.
#
# Same pattern as inject-connection-status-script.sh: the dashboard already
# loads plain (non-module) scripts next to the bundle, so the second-display
# prompt bar is added the same way instead of editing the minified bundle.
#
# Unlike its siblings, this script has no "required API" check against the
# bundle: it never touches window.webrtcInput, the settings postMessage
# channel, or any bundle-created DOM element — it only talks to fetch(),
# window.open() and its own loopback status endpoint, so there is nothing
# bundle-internal whose disappearance would need to fail the build here.
set -e

ROOT="${1:-/usr/share/selkies}"
SCRIPT_REL="src/wechat-second-display.js"
TAG="<script src=\"$SCRIPT_REL\"></script>"

found=0
for html in "$ROOT"/*/index.html; do
    [ -f "$html" ] || continue
    dir=$(dirname "$html")

    # Only dashboards that actually carry the streaming client.
    [ -f "$dir/$SCRIPT_REL" ] || continue

    tag_count=$(grep -Fo "$TAG" "$html" | wc -l | tr -d ' ')
    if [ "$tag_count" -gt 1 ]; then
        echo "inject-second-display-script: duplicate script tags in $html" >&2
        exit 1
    fi

    if [ "$tag_count" -eq 1 ]; then
        echo "inject-second-display-script: already present in $html"
        found=$((found + 1))
        continue
    fi

    if ! grep -q "</body>" "$html"; then
        echo "inject-second-display-script: no </body> in $html" >&2
        exit 1
    fi

    sed -i "s|</body>|  $TAG\n</body>|" "$html"

    tag_count=$(grep -Fo "$TAG" "$html" | wc -l | tr -d ' ')
    [ "$tag_count" -eq 1 ] || {
        echo "inject-second-display-script: FAILED to inject into $html" >&2
        exit 1
    }
    echo "inject-second-display-script: injected into $html"
    found=$((found + 1))
done

if [ "$found" -eq 0 ]; then
    echo "inject-second-display-script: found no dashboard with both index.html and $SCRIPT_REL" >&2
    exit 1
fi

echo "inject-second-display-script: $found dashboard(s) wired up"
