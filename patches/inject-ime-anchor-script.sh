#!/bin/sh
# Build-time patch: load patches/wechat-ime-anchor.js from the dashboard's HTML.
#
# Same pattern as inject-dragdrop-script.sh: the dashboard already loads plain
# (non-module) scripts next to the bundle, so the IME anchor script is added the
# same way instead of editing the minified bundle. It keeps #overlayInput as the
# pointer surface and adds a click-local textarea for native IME composition.
#
# /usr/share/selkies/web is deleted and recreated from /usr/share/selkies/$DASHBOARD
# by init-nginx on every container start, so both the HTML and the JS have to live
# in the dashboard directory, not in web/.
set -e

ROOT="${1:-/usr/share/selkies}"
SCRIPT_REL="src/wechat-ime-anchor.js"
TAG="<script src=\"$SCRIPT_REL\"></script>"

found=0
for html in "$ROOT"/*/index.html; do
    [ -f "$html" ] || continue
    dir=$(dirname "$html")
    bundle="$dir/src/selkies-core.js"

    # Only dashboards that actually carry the streaming client.
    [ -f "$dir/$SCRIPT_REL" ] || continue

    if [ ! -f "$bundle" ]; then
        echo "inject-ime-anchor-script: missing Selkies bundle beside $html" >&2
        exit 1
    fi

    for api in window.webrtcInput _compositionStart _compositionUpdate _compositionEnd; do
        if ! grep -Fq "$api" "$bundle"; then
            echo "inject-ime-anchor-script: required API $api missing from $bundle" >&2
            exit 1
        fi
    done

    tag_count=$(grep -Fo "$TAG" "$html" | wc -l | tr -d ' ')
    if [ "$tag_count" -gt 1 ]; then
        echo "inject-ime-anchor-script: duplicate script tags in $html" >&2
        exit 1
    fi

    if [ "$tag_count" -eq 1 ]; then
        echo "inject-ime-anchor-script: already present in $html"
        found=$((found + 1))
        continue
    fi

    if ! grep -q "</body>" "$html"; then
        echo "inject-ime-anchor-script: no </body> in $html" >&2
        exit 1
    fi

    # Last thing before </body>, so the bundle has already created
    # #overlayInput (or the script's own poll will find it shortly after).
    sed -i "s|</body>|  $TAG\n</body>|" "$html"

    tag_count=$(grep -Fo "$TAG" "$html" | wc -l | tr -d ' ')
    [ "$tag_count" -eq 1 ] || {
        echo "inject-ime-anchor-script: FAILED to inject into $html" >&2
        exit 1
    }
    echo "inject-ime-anchor-script: injected into $html"
    found=$((found + 1))
done

if [ "$found" -eq 0 ]; then
    echo "inject-ime-anchor-script: found no dashboard with both index.html and $SCRIPT_REL" >&2
    exit 1
fi

echo "inject-ime-anchor-script: $found dashboard(s) wired up"
