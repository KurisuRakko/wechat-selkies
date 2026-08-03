#!/bin/sh
# Build-time patch: load patches/wechat-dragdrop.js from the dashboard's HTML.
#
# The dashboard already loads one plain (non-module) script next to the bundle —
# src/universalTouchGamepad.js — so this follows the same pattern rather than
# editing the 666 KB minified bundle. The script itself only uses
# window.webrtcInput, which the bundle exports, so nothing minified is touched.
#
# /usr/share/selkies/web is deleted and recreated from /usr/share/selkies/$DASHBOARD
# by init-nginx on every container start, so both the HTML and the JS have to live
# in the dashboard directory, not in web/.
set -e

ROOT="${1:-/usr/share/selkies}"
SCRIPT_REL="src/wechat-dragdrop.js"
TAG="<script src=\"$SCRIPT_REL\"></script>"

found=0
for html in "$ROOT"/*/index.html; do
    [ -f "$html" ] || continue
    dir=$(dirname "$html")

    # Only dashboards that actually carry the streaming client.
    [ -f "$dir/$SCRIPT_REL" ] || continue

    if grep -q "wechat-dragdrop.js" "$html"; then
        echo "inject-dragdrop-script: already present in $html"
        found=$((found + 1))
        continue
    fi

    if ! grep -q "</body>" "$html"; then
        echo "inject-dragdrop-script: no </body> in $html" >&2
        exit 1
    fi

    # Last thing before </body>, so the bundle has already run and defined
    # window.webrtcInput by the time this executes.
    sed -i "s|</body>|  $TAG\n</body>|" "$html"

    grep -q "wechat-dragdrop.js" "$html" || {
        echo "inject-dragdrop-script: FAILED to inject into $html" >&2
        exit 1
    }
    echo "inject-dragdrop-script: injected into $html"
    found=$((found + 1))
done

if [ "$found" -eq 0 ]; then
    echo "inject-dragdrop-script: found no dashboard with both index.html and $SCRIPT_REL" >&2
    exit 1
fi

echo "inject-dragdrop-script: $found dashboard(s) wired up"
