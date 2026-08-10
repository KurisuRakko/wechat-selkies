#!/bin/sh
# Build-time patch: load patches/wechat-file-manager.js from the dashboard's HTML.
#
# Same pattern as inject-dragdrop-script.sh: the dashboard already loads plain
# (non-module) scripts next to the bundle, so this one is added the same way
# instead of editing the minified bundle. The script replaces the sidebar's
# "Files" modal iframe (whose nginx fancyindex listing is blank — see
# patches/files-json-index.py for the root cause) with a self-drawn file
# browser that reads the JSON listing.
#
# /usr/share/selkies/web is deleted and recreated from /usr/share/selkies/$DASHBOARD
# by init-nginx on every container start, so both the HTML and the JS have to live
# in the dashboard directory, not in web/.
set -e

ROOT="${1:-/usr/share/selkies}"
SCRIPT_REL="src/wechat-file-manager.js"
TAG="<script src=\"$SCRIPT_REL\"></script>"

found=0
for html in "$ROOT"/*/index.html; do
    [ -f "$html" ] || continue
    dir=$(dirname "$html")

    # Only dashboards that actually carry the streaming client.
    [ -f "$dir/$SCRIPT_REL" ] || continue

    tag_count=$(grep -Fo "$TAG" "$html" | wc -l | tr -d ' ')
    if [ "$tag_count" -gt 1 ]; then
        echo "inject-file-manager-script: duplicate script tags in $html" >&2
        exit 1
    fi

    if [ "$tag_count" -eq 1 ]; then
        echo "inject-file-manager-script: already present in $html"
        found=$((found + 1))
        continue
    fi

    if ! grep -q "</body>" "$html"; then
        echo "inject-file-manager-script: no </body> in $html" >&2
        exit 1
    fi

    # Last thing before </body>, so the bundle has already run by the time this
    # executes.
    sed -i "s|</body>|  $TAG\n</body>|" "$html"

    tag_count=$(grep -Fo "$TAG" "$html" | wc -l | tr -d ' ')
    [ "$tag_count" -eq 1 ] || {
        echo "inject-file-manager-script: FAILED to inject into $html" >&2
        exit 1
    }
    echo "inject-file-manager-script: injected into $html"
    found=$((found + 1))
done

if [ "$found" -eq 0 ]; then
    echo "inject-file-manager-script: found no dashboard with both index.html and $SCRIPT_REL" >&2
    exit 1
fi

echo "inject-file-manager-script: $found dashboard(s) wired up"
